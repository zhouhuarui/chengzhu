#!/usr/bin/env python3
"""Atomically import the validated Chengzhu Neo4j logical backup.

The importer deliberately supports only the small, fixed Chengzhu memory
schema. It refuses a non-empty target and never interpolates backup-provided
labels, relationship types, or property names into Cypher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase


EXPECTED_SCHEMA = 'chengzhu-neo4j-logical-backup-v1'
ALLOWED_LABELS = {'Group', 'Episode'}
ALLOWED_RELATIONSHIP_TYPE = 'HAS_EPISODE'
ALLOWED_PROPERTY_KEYS = {'id', 'body', 'group_id', 'meta_json', 'reference_time'}
TEMPORARY_ID_KEY = '__cz_migration_id'


def _fail(message: str) -> None:
    raise ValueError(message)


def _load_expected_sha256(path: Path) -> str:
    value = path.read_text(encoding='utf-8').strip().split()[0].lower()
    if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        _fail('invalid SHA-256 sidecar')
    return value


def _load_and_validate(backup_path: Path, sha256_path: Path) -> dict[str, Any]:
    raw = backup_path.read_bytes()
    expected_sha256 = _load_expected_sha256(sha256_path)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        _fail('logical backup SHA-256 mismatch')

    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get('schema') != EXPECTED_SCHEMA:
        _fail('unsupported logical backup schema')
    if payload.get('database') != 'neo4j':
        _fail('logical backup database must be neo4j')
    nodes = payload.get('nodes')
    relationships = payload.get('relationships')
    if not isinstance(nodes, list) or not isinstance(relationships, list):
        _fail('logical backup nodes/relationships must be arrays')

    element_labels: dict[str, str] = {}
    business_ids: dict[str, set[str]] = {label: set() for label in ALLOWED_LABELS}
    normalized_nodes: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            _fail(f'node {index} must be an object')
        element_id = node.get('element_id')
        labels = node.get('labels')
        properties = node.get('properties')
        if not isinstance(element_id, str) or not element_id:
            _fail(f'node {index} has invalid element_id')
        if element_id in element_labels:
            _fail(f'duplicate node element_id at index {index}')
        if not isinstance(labels, list) or len(labels) != 1 or labels[0] not in ALLOWED_LABELS:
            _fail(f'node {index} has unsupported labels')
        if not isinstance(properties, dict):
            _fail(f'node {index} properties must be an object')
        if TEMPORARY_ID_KEY in properties or not set(properties).issubset(ALLOWED_PROPERTY_KEYS):
            _fail(f'node {index} has unsupported property keys')
        if any(not isinstance(value, str) for value in properties.values()):
            _fail(f'node {index} properties must all be strings')
        business_id = properties.get('id')
        if not isinstance(business_id, str) or not business_id:
            _fail(f'node {index} is missing its business id')
        label = labels[0]
        if business_id in business_ids[label]:
            _fail(f'duplicate {label}.id at index {index}')
        business_ids[label].add(business_id)
        element_labels[element_id] = label
        normalized_nodes.append({
            'element_id': element_id,
            'label': label,
            'properties': properties,
        })

    normalized_relationships: list[dict[str, str]] = []
    relationship_triples: set[tuple[str, str, str]] = set()
    incoming_episode_count: Counter[str] = Counter()
    for index, relationship in enumerate(relationships):
        if not isinstance(relationship, dict):
            _fail(f'relationship {index} must be an object')
        rel_type = relationship.get('type')
        start_id = relationship.get('start_element_id')
        end_id = relationship.get('end_element_id')
        properties = relationship.get('properties')
        if rel_type != ALLOWED_RELATIONSHIP_TYPE:
            _fail(f'relationship {index} has unsupported type')
        if properties != {}:
            _fail(f'relationship {index} must not have properties')
        if start_id not in element_labels or end_id not in element_labels:
            _fail(f'relationship {index} has an unknown endpoint')
        if element_labels[start_id] != 'Group' or element_labels[end_id] != 'Episode':
            _fail(f'relationship {index} has invalid endpoint labels')
        triple = (start_id, rel_type, end_id)
        if triple in relationship_triples:
            _fail(f'duplicate relationship at index {index}')
        relationship_triples.add(triple)
        incoming_episode_count[end_id] += 1
        normalized_relationships.append({'start_id': start_id, 'end_id': end_id})

    episode_ids = {
        node['element_id'] for node in normalized_nodes if node['label'] == 'Episode'
    }
    if set(incoming_episode_count) != episode_ids:
        _fail('every Episode must have a HAS_EPISODE relationship')
    if any(count != 1 for count in incoming_episode_count.values()):
        _fail('every Episode must have exactly one incoming HAS_EPISODE relationship')

    return {
        'groups': [node for node in normalized_nodes if node['label'] == 'Group'],
        'episodes': [node for node in normalized_nodes if node['label'] == 'Episode'],
        'relationships': normalized_relationships,
        'expected_nodes': len(normalized_nodes),
        'expected_relationships': len(normalized_relationships),
        'expected_property_entries': sum(
            len(node['properties']) for node in normalized_nodes
        ),
    }


def _single_int(result: Any, key: str) -> int:
    record = result.single(strict=True)
    return int(record[key])


def _import_transaction(tx: Any, data: dict[str, Any]) -> dict[str, int]:
    current_nodes = _single_int(tx.run('MATCH (n) RETURN count(n) AS value'), 'value')
    current_relationships = _single_int(
        tx.run('MATCH ()-[r]->() RETURN count(r) AS value'), 'value'
    )
    if current_nodes or current_relationships:
        _fail('target Neo4j database is not empty')

    groups_created = _single_int(tx.run(
        '''UNWIND $rows AS row
           CREATE (node:Group)
           SET node = row.properties
           SET node.__cz_migration_id = row.element_id
           RETURN count(node) AS value''',
        rows=data['groups'],
    ), 'value')
    episodes_created = _single_int(tx.run(
        '''UNWIND $rows AS row
           CREATE (node:Episode)
           SET node = row.properties
           SET node.__cz_migration_id = row.element_id
           RETURN count(node) AS value''',
        rows=data['episodes'],
    ), 'value')
    relationships_created = _single_int(tx.run(
        '''UNWIND $rows AS row
           MATCH (group:Group {__cz_migration_id: row.start_id})
           MATCH (episode:Episode {__cz_migration_id: row.end_id})
           CREATE (group)-[relationship:HAS_EPISODE]->(episode)
           RETURN count(relationship) AS value''',
        rows=data['relationships'],
    ), 'value')

    if groups_created != len(data['groups']):
        _fail('not all Group nodes were created')
    if episodes_created != len(data['episodes']):
        _fail('not all Episode nodes were created')
    if relationships_created != data['expected_relationships']:
        _fail('not all relationships were created')

    removed_temporary_ids = _single_int(tx.run(
        'MATCH (node) REMOVE node.__cz_migration_id RETURN count(node) AS value'
    ), 'value')
    if removed_temporary_ids != data['expected_nodes']:
        _fail('temporary migration ids were not removed from every node')

    counts = tx.run(
        '''CALL () { MATCH (node) RETURN count(node) AS nodes }
           CALL () { MATCH ()-[relationship]->() RETURN count(relationship) AS relationships }
           CALL () { MATCH (episode:Episode) RETURN count(episode) AS episodes,
                         count(DISTINCT episode.id) AS unique_episode_ids }
           CALL () { MATCH (group:Group) RETURN count(group) AS groups,
                         count(DISTINCT group.id) AS unique_group_ids }
           CALL () { MATCH (:Group)-[relationship:HAS_EPISODE]->(episode:Episode)
                  RETURN count(relationship) AS linked_relationships,
                         count(DISTINCT episode) AS linked_episodes }
           CALL () { MATCH (episode:Episode)
                  WHERE NOT (:Group)-[:HAS_EPISODE]->(episode)
                  RETURN count(episode) AS orphan_episodes }
           CALL () { MATCH (node) UNWIND keys(node) AS key
                  RETURN count(key) AS property_entries }
           CALL () { MATCH (node)
                  WHERE node.__cz_migration_id IS NOT NULL
                  RETURN count(node) AS temporary_ids }
           RETURN *'''
    ).single(strict=True)
    result = {key: int(counts[key]) for key in counts.keys()}
    expected = {
        'nodes': data['expected_nodes'],
        'relationships': data['expected_relationships'],
        'episodes': len(data['episodes']),
        'unique_episode_ids': len(data['episodes']),
        'groups': len(data['groups']),
        'unique_group_ids': len(data['groups']),
        'linked_relationships': data['expected_relationships'],
        'linked_episodes': len(data['episodes']),
        'orphan_episodes': 0,
        'property_entries': data['expected_property_entries'],
        'temporary_ids': 0,
    }
    if result != expected:
        _fail(f'post-import validation failed: {result!r}')
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('backup', type=Path)
    parser.add_argument('--sha256-file', required=True, type=Path)
    args = parser.parse_args()

    data = _load_and_validate(args.backup, args.sha256_file)
    uri = os.environ.get('NEO4J_URI', 'bolt://neo4j:7687')
    user = os.environ.get('NEO4J_USER', 'neo4j')
    password = os.environ.get('NEO4J_PASSWORD', '')
    database = os.environ.get('NEO4J_DATABASE', 'neo4j')
    if not password:
        _fail('NEO4J_PASSWORD is required')

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            constraints = session.run(
                '''SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
                   RETURN name, type, labelsOrTypes, properties'''
            ).data()
            if not any(
                row.get('name') == 'episode_id'
                and row.get('labelsOrTypes') == ['Episode']
                and row.get('properties') == ['id']
                and 'UNIQUE' in str(row.get('type') or '')
                for row in constraints
            ):
                _fail('required Episode.id uniqueness constraint is missing')
            result = session.execute_write(_import_transaction, data)

    print(
        'Neo4j logical import passed: '
        f'nodes={result["nodes"]}, relationships={result["relationships"]}, '
        f'groups={result["groups"]}, episodes={result["episodes"]}.'
    )


if __name__ == '__main__':
    main()
