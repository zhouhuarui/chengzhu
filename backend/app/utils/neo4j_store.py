"""Neo4j 直连存储（无 Graphiti/LLM 时仍可写入 episode 节点，满足连通验收）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import Config


def neo4j_available() -> bool:
    driver = None
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            Config.NEO4J_URI,
            auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
        )
        driver.verify_connectivity()
        return True
    except Exception:
        return False
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


class Neo4jEpisodeStore:
    def __init__(self, group_id: str):
        from neo4j import GraphDatabase
        self.group_id = group_id
        self.driver = GraphDatabase.driver(
            Config.NEO4J_URI,
            auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
        )

    def close(self):
        self.driver.close()

    def ensure_constraints(self):
        with self.driver.session() as session:
            session.run(
                'CREATE CONSTRAINT episode_id IF NOT EXISTS '
                'FOR (e:Episode) REQUIRE e.id IS UNIQUE'
            )

    def add_episode(self, episode_id: str, body: str, reference_time: str, meta: Dict[str, Any]):
        with self.driver.session() as session:
            session.run(
                '''
                MERGE (e:Episode {id: $id})
                SET e.body = $body, e.reference_time = $ref,
                    e.group_id = $gid, e.meta_json = $meta
                WITH e
                MERGE (g:Group {id: $gid})
                MERGE (g)-[:HAS_EPISODE]->(e)
                ''',
                id=episode_id, body=body[:8000], ref=reference_time or '',
                gid=self.group_id, meta=str(meta or {})[:2000],
            )

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self.driver.session() as session:
            result = session.run(
                '''
                MATCH (e:Episode {group_id: $gid})
                WHERE e.body CONTAINS $q
                RETURN e.id AS id, e.body AS body, e.reference_time AS reference_time
                LIMIT $limit
                ''',
                gid=self.group_id, q=query[:80], limit=limit,
            )
            return [dict(r) for r in result]

    def statistics(self) -> Dict[str, Any]:
        with self.driver.session() as session:
            rec = session.run(
                'MATCH (e:Episode {group_id: $gid}) RETURN count(e) AS n',
                gid=self.group_id,
            ).single()
            return {'episodes': int(rec['n'] if rec else 0), 'backend': 'neo4j'}

    def delete_group(self):
        with self.driver.session() as session:
            session.run(
                'MATCH (e:Episode {group_id: $gid}) DETACH DELETE e',
                gid=self.group_id,
            )
            session.run('MATCH (g:Group {id: $gid}) DETACH DELETE g', gid=self.group_id)
