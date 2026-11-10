"""XX-Net 原子部署器；任一文件失败时恢复本批次。"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


class DeploymentError(Exception):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_root(path, name):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise DeploymentError("UNEXPECTED_PATH")
    return path


def _validate_relative(relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DeploymentError("UNEXPECTED_PATH")
    path = Path(relative)
    if path.is_absolute() or path.as_posix() != relative or any(part in ("", ".", "..") for part in path.parts):
        raise DeploymentError("UNEXPECTED_PATH")
    return path


def _validate_descendant(root, relative, allow_missing=False):
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DeploymentError("UNEXPECTED_PATH")
        if not current.exists():
            if allow_missing:
                continue
            raise DeploymentError("UNEXPECTED_PATH")
    return current


def _copy_verified(source, target, expected_hash):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if _sha256(target) != expected_hash:
        raise DeploymentError("BACKUP_FAILED")


def _restore_file(target, backup, old_hash, staging):
    if old_hash is None:
        if target.exists():
            target.unlink()
        return
    recovery = staging / ("recovery-" + hashlib.sha256(str(target).encode()).hexdigest())
    _copy_verified(backup, recovery, old_hash)
    os.replace(recovery, target)
    if _sha256(target) != old_hash:
        raise DeploymentError("ROLLBACK_FAILED")


def deploy_files(source_root, target_root, files, expected_old_hashes, backup_root):
    source_root = _validate_root(source_root, "source")
    target_root = _validate_root(target_root, "target")
    backup_root = Path(backup_root)
    if not backup_root.is_absolute() or backup_root.is_symlink():
        raise DeploymentError("UNEXPECTED_PATH")
    if backup_root.exists() and (not backup_root.is_dir() or any(backup_root.iterdir())):
        raise DeploymentError("UNEXPECTED_PATH")
    if not isinstance(files, list) or not files or not isinstance(expected_old_hashes, dict):
        raise DeploymentError("UNEXPECTED_PATH")

    relatives = [_validate_relative(item) for item in files]
    if len(set(relatives)) != len(relatives) or set(expected_old_hashes) != set(files):
        raise DeploymentError("UNEXPECTED_PATH")

    sources = {}
    targets = {}
    old_hashes = {}
    new_hashes = {}
    for relative_text, relative in zip(files, relatives):
        source = _validate_descendant(source_root, relative)
        target = _validate_descendant(target_root, relative, allow_missing=True)
        if not source.is_file():
            raise DeploymentError("UNEXPECTED_PATH")
        old_hash = expected_old_hashes[relative_text]
        if target.exists():
            if not target.is_file() or not isinstance(old_hash, str) or _sha256(target) != old_hash:
                raise DeploymentError("OLD_HASH_MISMATCH")
        elif old_hash is not None:
            raise DeploymentError("OLD_HASH_MISMATCH")
        sources[relative_text] = source
        targets[relative_text] = target
        old_hashes[relative_text] = old_hash
        new_hashes[relative_text] = _sha256(source)

    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        for relative_text, relative in zip(files, relatives):
            if old_hashes[relative_text] is not None:
                _copy_verified(targets[relative_text], backup_root / relative, old_hashes[relative_text])
    except DeploymentError:
        raise
    except BaseException:
        raise DeploymentError("BACKUP_FAILED") from None

    staging = Path(tempfile.mkdtemp(prefix=".deploy-staging-", dir=target_root))
    applied = []
    try:
        for relative_text, relative in zip(files, relatives):
            staged = staging / relative
            _copy_verified(sources[relative_text], staged, new_hashes[relative_text])
        for relative_text, relative in zip(files, relatives):
            target = targets[relative_text]
            target.parent.mkdir(parents=True, exist_ok=True)
            _validate_descendant(target_root, relative, allow_missing=True)
            os.replace(staging / relative, target)
            applied.append(relative_text)
            if _sha256(target) != new_hashes[relative_text]:
                raise DeploymentError("DEPLOY_HASH_MISMATCH")
    except BaseException:
        try:
            for relative_text in reversed(applied):
                relative = Path(relative_text)
                _restore_file(
                    targets[relative_text], backup_root / relative,
                    old_hashes[relative_text], staging,
                )
        except BaseException:
            raise DeploymentError("ROLLBACK_FAILED") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        raise DeploymentError("DEPLOYMENT_FAILED") from None
    shutil.rmtree(staging)
    return {
        "status": "deployed",
        "files": list(files),
        "old_hashes": old_hashes,
        "new_hashes": new_hashes,
        "backup_root": str(backup_root),
    }
