"""Verify an uninstalled, extracted client archive. Integrity is not a signature."""
import hashlib
import json
from pathlib import Path

EXPECTED_FILES = {
    '.codex-plugin/plugin.json', '.workbuddy-plugin/plugin.json', '.mcp.json', 'workbuddy.mcp.json',
    'README.md', '.env.example', 'requirements-local.txt', 'skills/yzzh/SKILL.md', 'skills/yzzh/agents/openai.yaml',
    'scripts/start_local.py', 'scripts/register_launcher.py', 'scripts/setup.py', 'scripts/launch.py', 'scripts/host_config.py', 'scripts/validate_release.py',
    'runtime/hybrid_shared.py', 'runtime/workflow_core.py', 'runtime/long_video_core.py',
    'runtime/face_mosaic.py', 'runtime/depth_video.py', 'runtime/yzzh_local/__init__.py',
    'runtime/yzzh_local/account.py', 'runtime/yzzh_local/settings.py', 'runtime/yzzh_local/provider.py',
    'runtime/yzzh_local/launcher.py', 'runtime/yzzh_local/web/launch.js', 'runtime/yzzh_local/media.py', 'runtime/yzzh_local/original_media.py',
    'runtime/yzzh_local/runtime.py', 'runtime/yzzh_local/app.py', 'runtime/yzzh_local/mcp.py',
    'runtime/yzzh_local/web/index.html', 'runtime/yzzh_local/web/app.js', 'runtime/yzzh_local/web/style.css',
    'runtime/web_app.py', 'runtime/performance_analysis.py', 'runtime/seedance_web.py', 'runtime/scripts/demucs_wav_separate.py',
    'runtime/yzzh_local/original.py', 'runtime/yzzh_local/original_worker.py', 'runtime/yzzh_local/offline_audio.py', 'runtime/yzzh_local/web/portal.js',
}
EXPECTED_FILES.update('runtime/web/' + name for name in (
    'app.js', 'real_long_video.js', 'long_video.css', 'workflow_shell.css', 'character_library.js', 'scene.css',
    'upload_preview.js', 'wardrobe.css', 'wardrobe.html', 'person.html', 'long_video.html', 'clothing.css',
    'projects.js', 'scene.html', 'clothing.html', 'person.js', 'projects.html', 'multi.css', 'workflow_common.js',
    'projects.css', 'clothing.js', 'multi.html', 'real_long_video.html', 'scene.js', 'styles.css', 'person.css',
    'index.html', 'long_video.js', 'multi.js', 'project_registry.js', 'wardrobe.js'))


def relative_name(path, root):
    return path.relative_to(root).as_posix()


def validate(root):
    root = Path(root).resolve()
    inventory = json.loads((root / 'SHA256SUMS.json').read_text())
    if not isinstance(inventory, dict) or set(inventory) != EXPECTED_FILES:
        raise ValueError('Client inventory does not match the complete release structure')
    actual = {relative_name(p, root) for p in root.rglob('*') if p.is_file()}
    if actual != EXPECTED_FILES | {'SHA256SUMS.json'}:
        raise ValueError('Unexpected or missing files; validate before installing dependencies/models')
    for name, expected in inventory.items():
        path = root / name
        path.resolve().relative_to(root)
        if path.is_symlink() or any(part in {'yzzh_cloud', '.git', '.venv', 'runs', 'models'} or (part.startswith('.env') and name != '.env.example') for part in path.relative_to(root).parts):
            raise ValueError('Forbidden client archive path')
        if name == '.env.example':
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip() and not line.lstrip().startswith('#') and ('=' not in line or line.split('=',1)[1].strip()):
                    raise ValueError('Customer template must have empty values')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Hash mismatch: ' + name)
    return len(inventory)


if __name__ == '__main__':
    try:
        count = validate(Path(__file__).resolve().parents[1])
    except (ValueError, OSError, TypeError) as error:
        raise SystemExit('Client archive validation failed: ' + str(error))
    print(f'Client archive validation passed: {count} files. Live host/business acceptance is separate.')
