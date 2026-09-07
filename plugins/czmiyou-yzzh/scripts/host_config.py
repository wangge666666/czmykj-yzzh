"""Print an absolute MCP entry; never overwrite a user's existing host configuration."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--login-url", default="https://mch39t7vkw.coze.site/api/auth/login", help="HTTPS CZMIYOU /api/auth/login endpoint; no video gateway needed")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
print(json.dumps({"mcpServers": {"czmiyou-yzzh": {"command": sys.executable,
    "args": [str(root / "scripts" / "launch.py")],
    "env": {"YZZH_LOGIN_URL": args.login_url}}}}, ensure_ascii=False, indent=2))
