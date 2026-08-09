"""Standalone Chengzhu MCP HTTP process used by AgentTeams Workers."""

from app.config import Config
from app.mcp import create_mcp_app


def main() -> None:
    app = create_mcp_app()
    app.run(
        host=Config.AGENTTEAMS_MCP_HOST,
        port=Config.AGENTTEAMS_MCP_PORT,
        debug=False,
        threaded=True,
    )


if __name__ == '__main__':
    main()

