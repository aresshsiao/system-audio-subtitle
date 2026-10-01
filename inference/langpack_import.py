"""語言包匯入。見 ARCHITECTURE.md §13.4：**驗證 → 複製進使用者本機目錄 → 出現在
清單**，全程不執行任何包內代碼、不需要重啟主程式。

匯入的來源是使用者從網路或別人那裡拿到的檔案，所以這裡是整個系統少數幾個真正
面對「不可信輸入」的地方。防線：

  - 語言包是**純資料**：只接受 `.yaml/.yml/.tsv/.txt/.md`，其他副檔名（`.py`、
    `.exe`、`.dll`…）整包拒絕
  - zip 解壓縮擋 zip-slip（絕對路徑、`..`、磁碟機代號）、符號連結、檔案數量與
    解壓後大小上限（zip bomb）
  - `pack.yaml` 的 `id` 會變成資料夾名稱，所以限制成安全字元集，不然 `id: ../../x`
    就是路徑穿越
  - 引擎名稱、路徑逃逸等由 `contracts/langpack_schema.py` 驗證（白名單）
  - 不允許覆蓋內建語言包（同 id）
"""

from __future__ import annotations

import logging
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from contracts.langpack_schema import LangPackValidationError, LanguagePack, load_and_validate
from inference.langpack import DEFAULT_LANGPACKS_DIR
from utils.paths import user_langpacks_dir

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({".yaml", ".yml", ".tsv", ".txt", ".md"})
MAX_FILES = 50
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LangPackImportError(Exception):
    """匯入失敗。訊息是給使用者看的，要讓人看得懂哪裡錯、怎麼修。"""


@dataclass(frozen=True)
class ImportResult:
    pack: LanguagePack
    dest: Path
    replaced: bool  # 使用者目錄裡已有同 id 的包，被這次匯入取代


def _check_tree(root: Path) -> None:
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) > MAX_FILES:
        raise LangPackImportError(f"語言包檔案太多（{len(files)} > {MAX_FILES}），這不像是語言包")
    total = 0
    for f in files:
        rel = f.relative_to(root)
        if f.is_symlink():
            raise LangPackImportError(f"語言包不可包含符號連結: {rel}")
        if f.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise LangPackImportError(
                f"語言包只能包含資料檔（{', '.join(sorted(ALLOWED_EXTENSIONS))}），"
                f"但發現 '{rel}'——語言包不能含可執行內容"
            )
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            raise LangPackImportError(f"檔案過大: {rel}（{size} bytes）")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise LangPackImportError(f"語言包總大小過大（{total} bytes）")


def _safe_extract(zip_path: Path, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise LangPackImportError(f"不是有效的 zip 檔: {e}") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise LangPackImportError(f"壓縮檔內檔案太多（{len(infos)} > {MAX_FILES}）")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise LangPackImportError("解壓縮後總大小過大，拒絕匯入")
        dest_resolved = dest.resolve()
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in Path(name).parts:
                raise LangPackImportError(f"壓縮檔含不安全的路徑，拒絕匯入: {name!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise LangPackImportError(f"壓縮檔含符號連結，拒絕匯入: {name!r}")
            target = (dest / name).resolve()
            if dest_resolved not in target.parents:
                raise LangPackImportError(f"壓縮檔含不安全的路徑，拒絕匯入: {name!r}")
            if info.file_size > MAX_FILE_BYTES:
                raise LangPackImportError(f"檔案過大: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read(MAX_FILE_BYTES + 1))


def _find_pack_root(extracted: Path) -> Path:
    if (extracted / "pack.yaml").is_file():
        return extracted
    children = [p for p in extracted.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "pack.yaml").is_file():
        return children[0]
    raise LangPackImportError("找不到 pack.yaml（應該在壓縮檔根目錄，或唯一的一層資料夾裡）")


def import_langpack(
    source: Path | str,
    *,
    user_dir: Path | None = None,
    builtin_dir: Path = DEFAULT_LANGPACKS_DIR,
) -> ImportResult:
    """匯入資料夾或 `.zip`（含 `.langpack.zip`）。失敗一律丟 `LangPackImportError`，
    而且**失敗時不會在使用者目錄留下任何東西**。"""
    source = Path(source)
    user_dir = user_dir or user_langpacks_dir()
    if not source.exists():
        raise LangPackImportError(f"找不到: {source}")

    with tempfile.TemporaryDirectory(prefix="sas-langpack-") as tmp:
        staging = Path(tmp) / "pack"
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=True)
        elif source.suffix.lower() == ".zip":
            staging.mkdir()
            _safe_extract(source, staging)
        else:
            raise LangPackImportError("請選擇語言包資料夾，或 .langpack.zip / .zip 檔")

        pack_root = _find_pack_root(staging)
        _check_tree(pack_root)
        try:
            pack = load_and_validate(pack_root)
        except LangPackValidationError as e:
            raise LangPackImportError(f"語言包格式不合法：{e}") from e

        if not _SAFE_ID_RE.fullmatch(pack.id):
            raise LangPackImportError(
                f"語言包 id '{pack.id}' 不合法：只能用英數字、'.'、'_'、'-'，且以英數字開頭"
            )
        builtin_ids = {p.name for p in builtin_dir.iterdir() if p.is_dir()} if builtin_dir.is_dir() else set()
        if pack.id in builtin_ids:
            raise LangPackImportError(f"id '{pack.id}' 與內建語言包相同，請改一個不同的 id")

        user_dir.mkdir(parents=True, exist_ok=True)
        dest = user_dir / pack.id
        replaced = dest.exists()
        new_dest = user_dir / f".{pack.id}.new"
        if new_dest.exists():
            shutil.rmtree(new_dest)
        shutil.copytree(pack_root, new_dest)
        try:
            if replaced:
                shutil.rmtree(dest)
            new_dest.rename(dest)
        except OSError as e:
            shutil.rmtree(new_dest, ignore_errors=True)
            raise LangPackImportError(f"寫入使用者目錄失敗: {e}") from e

    # 從最終位置重新驗證一次：glossary 等路徑是相對於包目錄解析的，要以安裝後的位置為準
    final_pack = load_and_validate(dest)
    logger.info("語言包已匯入: %s -> %s（%s）", final_pack.id, dest, "取代舊版" if replaced else "新增")
    return ImportResult(pack=final_pack, dest=dest, replaced=replaced)


def remove_user_langpack(pack_id: str, *, user_dir: Path | None = None) -> bool:
    """移除使用者匯入的包（內建包不受影響）。回傳是否真的刪了東西。"""
    user_dir = user_dir or user_langpacks_dir()
    if not _SAFE_ID_RE.fullmatch(pack_id):
        return False
    target = user_dir / pack_id
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False
