"""Build a self-contained client-only ZIP using explicit file allowlists."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "czmiyou-yzzh"
ENGINE = ["hybrid_shared.py", "workflow_core.py", "long_video_core.py", "face_mosaic.py", "depth_video.py", "web_app.py", "performance_analysis.py", "seedance_web.py", "scripts/demucs_wav_separate.py"]
CLIENT = ["__init__.py", "account.py", "settings.py", "provider.py", "platform.py", "platform_service.py", "platform_bridge.py", "workflow_approval.py", "launcher.py", "web/launch.js", "media.py", "original_media.py", "runtime.py", "app.py", "mcp.py", "original.py", "original_worker.py", "offline_audio.py", "web/index.html", "web/app.js", "web/style.css", "web/portal.js"]
WEB = ["app.js", "real_long_video.js", "long_video.css", "workflow_shell.css", "character_library.js", "scene.css", "upload_preview.js", "wardrobe.css", "wardrobe.html", "person.html", "long_video.html", "clothing.css", "projects.js", "scene.html", "clothing.html", "person.js", "projects.html", "multi.css", "workflow_common.js", "projects.css", "clothing.js", "multi.html", "real_long_video.html", "scene.js", "styles.css", "person.css", "index.html", "long_video.js", "multi.js", "project_registry.js", "wardrobe.js"]
ENGINE += ['cast_colors.py', 'inline_cast.py', 'motion_transfer.py', 'motion_video.py', 'multi_cast.py', 'video_proxy_rules.py', 'wardrobe_audio.py', 'wardrobe_continuation.py', 'wardrobe_dynamic.py', 'wardrobe_full_rewrite.py', 'wardrobe_intervals.py', 'wardrobe_object.py', 'wardrobe_rewrite_sources.py', 'wardrobe_segments.py']
WEB += ['cast_entry.js', 'character_library.css', 'inline_cast.js', 'motion_transfer.css', 'motion_transfer.html', 'motion_transfer.js', 'wardrobe_audio.js', 'wardrobe_continuation.css', 'wardrobe_continuation.html', 'wardrobe_continuation.js', 'wardrobe_object.html', 'wardrobe_object.js', 'wardrobe_scene.html', 'wardrobe_segments.js']
PACKAGE = [".codex-plugin/plugin.json", ".workbuddy-plugin/plugin.json", ".mcp.json", "workbuddy.mcp.json",
           "README.md", ".env.example", "requirements-local.txt", "skills/yzzh/SKILL.md", "skills/yzzh/agents/openai.yaml", "scripts/launch.py", "scripts/start_local.py", "scripts/register_launcher.py", "scripts/setup.py", "scripts/host_config.py", "scripts/validate_release.py"]


def entries():
    items = [(PLUGIN / file, file) for file in PACKAGE]
    items += [(ROOT / file, "runtime/" + file) for file in ENGINE]
    items += [(ROOT / "yzzh_local" / file, "runtime/yzzh_local/" + file) for file in CLIENT]
    items += [(ROOT / "web" / file, "runtime/web/" + file) for file in WEB]
    return items


def build(target):
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    inventory = {}
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in entries():
            content = source.read_bytes()
            if name == ".env.example":
                for line in content.decode("utf-8").splitlines():
                    if line.strip() and not line.lstrip().startswith("#") and ("=" not in line or line.split("=", 1)[1].strip()):
                        raise ValueError("Customer template must have empty values")
            inventory[name] = hashlib.sha256(content).hexdigest()
            archive.writestr("czmiyou-yzzh/" + name, content)
        archive.writestr("czmiyou-yzzh/SHA256SUMS.json", json.dumps(inventory, indent=2) + "\n")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="New ZIP filename; existing files are never overwritten")
    args = parser.parse_args()
    print(build(args.output))
