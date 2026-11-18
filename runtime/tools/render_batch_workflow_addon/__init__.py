"""Inspect local blenderctl render batches and request explicit CLI recovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import bpy
from bpy.props import StringProperty

LABELS = {
    "queued": "排队",
    "freezing": "冻结输入",
    "running": "执行中",
    "preflight": "预检",
    "preview_rendering": "渲染预览",
    "preview_encoding": "验证预览",
    "final_rendering": "渲染终稿",
    "final_encoding": "验证终稿",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "paused": "暂停",
}
RECOVERABLE_STATES = frozenset({"running", "paused", "partial", "failed"})
_directory_property = "render_batch_directory"
_launcher_property = "render_batch_launcher"
_jobs_root_property = "render_batch_jobs_root"

STATUS_LIMIT = 1024 * 1024
LAUNCH_LIMIT = 4 * 1024 * 1024
SCAN_DIRECTORY_LIMIT = 4096
SCAN_PARSE_LIMIT = 256
CLI_LOG_NAME = "panel-cli.log"

_launch_cache = {}
_cached = {
    "input": "",
    "root": None,
    "status": None,
    "status_error": None,
    "launch": None,
    "launch_error": None,
    "cli_tail": "",
    "cli_log": None,
}


def _empty_cache(input_text=""):
    return {
        "input": input_text,
        "root": None,
        "status": None,
        "status_error": None,
        "launch": None,
        "launch_error": None,
        "cli_tail": "",
        "cli_log": None,
    }


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        if not value:
            return ""
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    return text.replace("\x00", "").strip()


def _name(value):
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    return text or None


def _non_negative_int(value):
    return value if type(value) is int and value >= 0 else None


def _frame_number(value):
    return value if type(value) is int and 0 <= value <= 100000 else None


def _path_name(value):
    text = _text(value)
    if not text:
        return ""
    try:
        return Path(text).name or text
    except (OSError, ValueError):
        return text


def batch_directory(value):
    text = "" if value is None else str(value)
    if not text.strip():
        raise ValueError("选择 CLI 回执中的批次目录")
    try:
        path = Path(bpy.path.abspath(text)).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"批次目录无效: {error}") from error
    if not path.is_dir():
        raise ValueError("批次目录不存在")
    return path


def _read_json_file(path, limit, label):
    path = Path(path)
    raw = None
    for attempt in (0, 1):
        try:
            with path.open("rb") as handle:
                raw = handle.read(limit + 1)
            break
        except PermissionError:
            if attempt:
                raise
            time.sleep(0.05)
    if raw is None:
        raise OSError(f"无法读取 {label}")
    if len(raw) > limit:
        raise ValueError(f"{label} 超出面板可读取范围")
    if not raw.strip():
        raise ValueError(f"{label} 为空")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{label} 不是有效 JSON：{error}") from error


def read_batch_status(directory):
    """Read the public status record, accepting legacy missing optional fields."""
    root = batch_directory(str(directory) if directory is not None else "")
    path = root / "status.json"
    if not path.is_file():
        raise ValueError("此批次没有 status.json")
    if path.is_symlink():
        raise ValueError("status.json 不能是符号链接")
    data = _read_json_file(path, STATUS_LIMIT, "status.json")
    if not isinstance(data, dict):
        raise ValueError("status.json 顶层不是 JSON 对象")

    warnings = []
    version = data.get("version")
    if version is not None and version != "1.0":
        raise ValueError("status.json 的版本不是 1.0")
    raw_batch = data.get("batch_id")
    batch_id = _name(raw_batch)
    if raw_batch is not None and batch_id is None:
        warnings.append("status.json 的 batch_id 不是字符串")
    if batch_id is not None and batch_id != root.name:
        raise ValueError(f"status.json 的批次 {batch_id} 与目录 {root.name} 不符")

    raw_state = data.get("state")
    state = _name(raw_state) or ""
    if raw_state is not None and not isinstance(raw_state, str):
        warnings.append("status.json 的 state 不是字符串")

    raw_shots = data.get("shots")
    if raw_shots is None:
        warnings.append("status.json 缺少 shots，无法显示镜头进度")
        raw_shots = []
    elif not isinstance(raw_shots, list):
        raise ValueError("status.json 的 shots 不是列表")

    shots = []
    missing_progress = 0
    seen_ids = set()
    for index, item in enumerate(raw_shots):
        if not isinstance(item, dict):
            warnings.append(f"跳过第 {index + 1} 条非对象镜头状态")
            continue
        ident = _name(item.get("id"))
        if ident is None:
            warnings.append(f"跳过第 {index + 1} 条缺少 id 的镜头状态")
            continue
        key = ident.casefold()
        if key in seen_ids:
            warnings.append(f"镜头 id 重复: {ident}")
        seen_ids.add(key)
        shot_state = _name(item.get("state")) or ""
        if not shot_state:
            warnings.append(f"镜头 {ident} 缺少状态")
        completed = _non_negative_int(item.get("completed_frames"))
        total = _non_negative_int(item.get("total_frames"))
        if completed is None or total is None:
            missing_progress += 1
        row = dict(item)
        row.update(id=ident, state=shot_state, completed_frames=completed, total_frames=total)
        shots.append(row)
    if missing_progress:
        warnings.append(f"{missing_progress} 个镜头缺少进度字段，已按可用信息显示")

    status = dict(data)
    status.update(
        version=version or "1.0",
        batch_id=batch_id or root.name,
        state=state,
        shots=shots,
        panel_warnings=warnings,
    )
    return status


def read_batch_launch(directory):
    """Read launch.json's requested shots without requiring every optional field."""
    root = batch_directory(str(directory) if directory is not None else "")
    path = root / "launch.json"
    if not path.is_file():
        raise ValueError("此批次没有 launch.json，无法显示已提交请求")
    if path.is_symlink():
        raise ValueError("launch.json 不能是符号链接")
    data = _read_json_file(path, LAUNCH_LIMIT, "launch.json")
    if not isinstance(data, dict):
        raise ValueError("launch.json 顶层不是 JSON 对象")

    warnings = []
    manifest = data.get("manifest")
    if isinstance(manifest, dict):
        raw_shots = manifest.get("shots")
        if raw_shots is None and isinstance(data.get("shots"), list):
            raw_shots = data["shots"]
            warnings.append("launch.json 使用顶层 shots（兼容旧记录）")
    elif manifest is not None:
        warnings.append("launch.json 的 manifest 不是对象，尝试顶层 shots")
        raw_shots = data.get("shots")
    else:
        raw_shots = data.get("shots")
        if isinstance(raw_shots, list):
            warnings.append("launch.json 使用顶层 shots（兼容旧记录）")
    if not isinstance(raw_shots, list):
        raise ValueError("launch.json 缺少 manifest.shots 列表")

    shots = []
    for index, item in enumerate(raw_shots):
        if not isinstance(item, dict):
            warnings.append(f"跳过第 {index + 1} 条非对象请求镜头")
            continue
        ident = _name(item.get("id"))
        if ident is None:
            ident = f"#{index + 1}"
            warnings.append(f"第 {index + 1} 条请求镜头缺少 id，使用 {ident}")

        project = item.get("project")
        if isinstance(project, str) and _text(project):
            project = _text(project)
        else:
            project = None
            warnings.append(f"请求镜头 {ident} 缺少 project")
        scene = _name(item.get("scene"))
        view_layer = _name(item.get("view_layer"))
        camera = _name(item.get("camera"))
        missing_names = [
            label for label, value in
            (("scene", scene), ("view_layer", view_layer), ("camera", camera))
            if value is None
        ]
        if missing_names:
            warnings.append(f"请求镜头 {ident} 缺少 {'/'.join(missing_names)}")

        start = _frame_number(item.get("frame_start"))
        end = _frame_number(item.get("frame_end"))
        if start is None and item.get("frame_start") is not None:
            warnings.append(f"请求镜头 {ident} 的 frame_start 无效")
        if end is None and item.get("frame_end") is not None:
            warnings.append(f"请求镜头 {ident} 的 frame_end 无效")
        if start is not None and end is not None and end < start:
            warnings.append(f"请求镜头 {ident} 的帧段倒置")

        shots.append({
            "id": ident,
            "project": project,
            "scene": scene,
            "view_layer": view_layer,
            "camera": camera,
            "frame_start": start,
            "frame_end": end,
        })

    launch = dict(data)
    launch.update(shots=shots, panel_warnings=warnings)
    return launch


def _launch_for(root):
    path = root / "launch.json"
    if not path.is_file():
        return None, "此批次没有 launch.json，无法显示已提交请求"
    if path.is_symlink():
        return None, "launch.json 不能是符号链接"
    try:
        stat = path.stat()
    except OSError as error:
        return None, f"无法读取 launch.json: {error}"
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(root)
    cached = _launch_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    try:
        launch = read_batch_launch(root)
        error = None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        launch = None
        error = str(exc)
    if len(_launch_cache) >= 16:
        _launch_cache.clear()
    _launch_cache[key] = (signature, launch, error)
    return launch, error


def _read_cli_tail(path):
    if path.is_symlink():
        return ""
    try:
        stat = path.stat()
        if not stat.st_size:
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - 8192))
            raw = handle.read(8192)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace").replace("\x00", "")
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    for line in reversed(lines):
        if line.startswith("==="):
            return ""
        if line.startswith("command:"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return line[:240]
        if isinstance(record, dict):
            error = record.get("error")
            if isinstance(error, dict):
                code = _name(error.get("code")) or "ERROR"
                message = _text(error.get("message")) or "CLI 拒绝了请求"
                return f"{code}: {message}"[:240]
            if record.get("ok") is True:
                return "CLI 已接受请求"
        return line[:240]
    return ""


def _redraw_views():
    try:
        manager = bpy.context.window_manager
        if manager is None:
            return
        for window in manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        return


def refresh(directory):
    global _cached
    text = "" if directory is None else str(directory)
    cache = _empty_cache(text)
    if text.strip():
        try:
            root = batch_directory(text)
        except (OSError, ValueError, RuntimeError) as error:
            cache["status_error"] = str(error)
        else:
            cache["root"] = root
            try:
                cache["status"] = read_batch_status(root)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                message = str(error)
                cache["status_error"] = (
                    message if message.startswith(("此批次", "status.json"))
                    else f"status.json 无法读取: {message}"
                )
            cache["launch"], cache["launch_error"] = _launch_for(root)
            log = root / CLI_LOG_NAME
            cache["cli_log"] = log
            cache["cli_tail"] = _read_cli_tail(log)
    _cached = cache
    _redraw_views()


def tick():
    global _cached
    try:
        manager = bpy.context.window_manager
        directory = getattr(manager, _directory_property, "")
        if isinstance(directory, str) and directory.strip():
            refresh(directory)
        elif _cached.get("input"):
            _cached = _empty_cache()
            _redraw_views()
    except Exception:
        pass
    return 2.0


def resolve_batch_artifact(directory, value):
    root = batch_directory(str(directory) if directory is not None else "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("此产物尚未发布")
    relative = Path(value)
    if (relative.is_absolute() or relative.anchor
            or ".." in relative.parts or any(":" in part for part in relative.parts)):
        raise ValueError("产物路径须在当前批次中")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() != ".mp4":
        raise ValueError("找不到批次内可播放的视频")
    return path


def _project_key(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("\x00", "").strip()
    if not text:
        return None
    try:
        path = Path(bpy.path.abspath(text))
    except (OSError, RuntimeError, ValueError):
        try:
            path = Path(os.path.abspath(text))
        except (OSError, ValueError):
            return None
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        try:
            resolved = Path(os.path.abspath(str(path)))
        except (OSError, ValueError):
            return None
    try:
        return os.path.normcase(os.path.normpath(str(resolved)))
    except (OSError, ValueError):
        return None


def _effective_jobs_root(manager, batch_root=None):
    raw = getattr(manager, _jobs_root_property, "")
    if isinstance(raw, str) and raw.strip():
        try:
            path = Path(bpy.path.abspath(raw)).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(f"Jobs 根目录无效: {error}") from error
        if not path.is_dir():
            raise ValueError("Jobs 根目录不存在")
        return path.parent if path.name.casefold() == "b" else path
    if batch_root is None:
        raw_batch = getattr(manager, _directory_property, "")
        if isinstance(raw_batch, str) and raw_batch.strip():
            try:
                batch_root = batch_directory(raw_batch)
            except (OSError, ValueError, RuntimeError):
                batch_root = None
    if batch_root is not None and batch_root.parent.name.casefold() == "b":
        return batch_root.parent.parent
    return None


def find_latest_project_batch(jobs_root, filepath):
    project_key = _project_key(filepath)
    if project_key is None:
        raise ValueError("当前工程尚未保存，无法扫描批次")
    b_directory = Path(jobs_root) / "b"
    if not b_directory.is_dir():
        raise ValueError("Jobs 根目录下没有 b 子目录")

    candidates = []
    inspected = 0
    truncated = False
    try:
        with os.scandir(b_directory) as entries:
            for entry in entries:
                inspected += 1
                if inspected > SCAN_DIRECTORY_LIMIT:
                    truncated = True
                    break
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                path = Path(entry.path)
                launch_path = path / "launch.json"
                if launch_path.is_symlink():
                    continue
                try:
                    stat = launch_path.stat()
                except OSError:
                    continue
                if not 0 < stat.st_size <= LAUNCH_LIMIT:
                    continue
                candidates.append((stat.st_mtime_ns, stat.st_mtime, path))
    except OSError as error:
        raise ValueError(f"无法扫描 Jobs 根目录: {error}") from error

    candidates.sort(key=lambda row: (row[0], row[2].name), reverse=True)
    parsed = 0
    for _, modified, path in candidates:
        if parsed >= SCAN_PARSE_LIMIT:
            truncated = True
            break
        parsed += 1
        try:
            launch = read_batch_launch(path)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            continue
        if any(_project_key(shot.get("project")) == project_key for shot in launch["shots"]):
            return {
                "path": path,
                "parsed": parsed,
                "candidates": len(candidates),
                "truncated": truncated,
                "modified_unix": modified,
            }
    return {
        "path": None,
        "parsed": parsed,
        "candidates": len(candidates),
        "truncated": truncated,
        "modified_unix": None,
    }


def _launch_total(shot):
    if not isinstance(shot, dict):
        return None
    start = _frame_number(shot.get("frame_start"))
    end = _frame_number(shot.get("frame_end"))
    if start is None or end is None or end < start:
        return None
    return end - start + 1


def _merged_shots(status, launch):
    status_rows = status.get("shots", []) if isinstance(status, dict) else []
    if not isinstance(status_rows, list):
        status_rows = []
    by_id = {}
    for row in status_rows:
        if isinstance(row, dict):
            ident = _name(row.get("id"))
            if ident:
                by_id.setdefault(ident.casefold(), row)

    claimed = set()
    merged = []
    launch_rows = launch.get("shots", []) if isinstance(launch, dict) else []
    if not isinstance(launch_rows, list):
        launch_rows = []
    for row in launch_rows:
        if not isinstance(row, dict):
            continue
        ident = _name(row.get("id"))
        key = ident.casefold() if ident else None
        status_row = by_id.get(key) if key and key not in claimed else None
        if status_row is not None:
            claimed.add(key)
        merged.append((row, status_row))
    for row in status_rows:
        if not isinstance(row, dict):
            continue
        key = (_name(row.get("id")) or "").casefold()
        if key and key in claimed:
            continue
        merged.append((None, row))
    return merged


def _progress_values(status_shot, launch_shot):
    completed = None
    total = None
    if isinstance(status_shot, dict):
        completed = _non_negative_int(status_shot.get("completed_frames"))
        total = _non_negative_int(status_shot.get("total_frames"))
    if total is None or total <= 0:
        total = _launch_total(launch_shot)
    return completed, total


def _shot_progress_text(completed, total):
    if total:
        if completed is not None:
            percent = min(100, max(0, int(completed * 100 / total)))
            return f"{completed}/{total} 帧 · {percent}%"
        return f"请求 {total} 帧 · 进度未报告"
    if completed is not None:
        return f"已完成 {completed} 帧 · 总数未知"
    return "进度未知"


def _batch_progress_text(status, launch):
    rows = _merged_shots(status, launch)
    if not rows:
        return ""
    completed = 0
    total = 0
    known_total = 0
    unknown_total = 0
    known_completed = True
    reported = 0
    for launch_shot, status_shot in rows:
        if isinstance(status_shot, dict):
            reported += 1
        shot_completed, shot_total = _progress_values(status_shot, launch_shot)
        if shot_completed is None:
            known_completed = False
        else:
            completed += shot_completed
        if shot_total and shot_total > 0:
            total += shot_total
            known_total += 1
        else:
            unknown_total += 1
    if not reported:
        if known_total and not unknown_total:
            return f"请求共 {total} 帧 · 尚未报告进度"
        return "请求已记录 · 尚未报告进度"
    if known_total and not unknown_total:
        prefix = "" if known_completed else "至少 "
        percent = min(100, max(0, int(completed * 100 / total))) if total else 0
        return f"批次进度 {prefix}{completed}/{total} 帧 · {percent}%"
    if known_total:
        return f"批次已知完成 {completed} 帧 · {unknown_total} 个镜头帧数未知"
    if known_completed:
        return f"批次已完成 {completed} 帧 · 总帧数未知"
    return "批次进度未报告"


def _frame_range_text(shot):
    if not isinstance(shot, dict):
        return "未记录"
    start = _frame_number(shot.get("frame_start"))
    end = _frame_number(shot.get("frame_end"))
    if start is not None and end is not None:
        if end < start:
            return f"{start}–{end}（帧段无效）"
        return f"{start}–{end}（{end - start + 1} 帧）"
    if start is not None:
        return f"起 {start} · 止未记录"
    if end is not None:
        return f"止 {end} · 起未记录"
    return "未记录"


def _status_by_id(status):
    rows = status.get("shots", []) if isinstance(status, dict) else []
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                ident = _name(row.get("id"))
                if ident:
                    result.setdefault(ident.casefold(), row)
    return result


def _matching_launch_shots(launch, filepath):
    key = _project_key(filepath)
    if key is None or not isinstance(launch, dict):
        return []
    rows = launch.get("shots", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and _project_key(row.get("project")) == key]


def _pending_count(matched, status):
    if not isinstance(status, dict):
        return None
    by_id = _status_by_id(status)
    if not by_id:
        return None
    pending = 0
    for shot in matched:
        ident = _name(shot.get("id"))
        row = by_id.get(ident.casefold()) if ident else None
        if row is None or _name(row.get("state")) != "completed":
            pending += 1
    return pending


def _current_project_summary(context):
    filepath = _text(getattr(bpy.data, "filepath", ""))
    scene = getattr(context, "scene", None)
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None and scene is not None:
        try:
            view_layer = scene.view_layers.active
        except Exception:
            view_layer = None
    scene_name = _name(getattr(scene, "name", None)) or "无"
    camera_name = "无"
    camera = getattr(scene, "camera", None) if scene is not None else None
    if camera is not None:
        camera_name = _name(getattr(camera, "name", None)) or "无"
    view_layer_name = _name(getattr(view_layer, "name", None)) or "无"
    fps = None
    if scene is not None:
        try:
            fps_base = scene.render.fps_base or 1.0
            fps = scene.render.fps / fps_base
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            fps = None
    file_name = folder_name = ""
    if filepath:
        try:
            path = Path(filepath)
            file_name = path.name
            folder_name = path.parent.name
        except (OSError, ValueError):
            file_name = _path_name(filepath)
    return {
        "filepath": filepath,
        "file_name": file_name,
        "folder_name": folder_name,
        "scene": scene_name,
        "camera": camera_name,
        "view_layer": view_layer_name,
        "frame_start": getattr(scene, "frame_start", None) if scene is not None else None,
        "frame_end": getattr(scene, "frame_end", None) if scene is not None else None,
        "frame_current": getattr(scene, "frame_current", None) if scene is not None else None,
        "fps": fps,
    }


def _draw_text(layout, value, icon=None, max_lines=4, limit=240, prefix=""):
    text = _text(value)
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = [text]
    for index, line in enumerate(lines[:max_lines]):
        shown = (prefix if index == 0 else "") + line[:limit]
        if len(line) > limit:
            shown += "…"
        if icon and index == 0:
            layout.label(text=shown, icon=icon)
        else:
            layout.label(text=shown)
    if len(lines) > max_lines:
        layout.label(text=f"… 另有 {len(lines) - max_lines} 行，请打开批次文件查看", icon="INFO")
    return True


def _draw_warnings(layout, warnings):
    unique = []
    for item in warnings or []:
        text = _text(item)
        if text and text not in unique:
            unique.append(text)
    if not unique:
        return
    for text in unique[:3]:
        layout.label(text=text[:200], icon="INFO")
    if len(unique) > 3:
        layout.label(text=f"另有 {len(unique) - 3} 条记录提示，请打开批次文件查看", icon="INFO")


def _draw_current_project(layout, context, cache):
    summary = _current_project_summary(context)
    box = layout.box()
    box.label(text="当前工程（打开的 .blend）", icon="INFO")
    start = summary["frame_start"] if type(summary["frame_start"]) is int else "?"
    end = summary["frame_end"] if type(summary["frame_end"]) is int else "?"
    current = summary["frame_current"] if type(summary["frame_current"]) is int else "?"
    fps_text = ""
    if summary["fps"] is not None:
        fps_text = f" · {summary['fps']:g} fps"
    if summary["filepath"]:
        where = summary["file_name"]
        if summary["folder_name"]:
            where = f"{where} · {summary['folder_name']}"
        box.label(text=where)
        box.label(text=f"当前场景: {summary['scene']} · 当前相机: {summary['camera']}")
        box.label(text=f"当前帧段: {start}–{end} · 当前帧: {current}{fps_text}")
        box.label(text=f"当前视图层: {summary['view_layer']}")
    else:
        box.label(text="未保存工程：无法与 launch.json 的绝对项目路径匹配", icon="INFO")
        box.label(text=f"当前场景: {summary['scene']} · 当前相机: {summary['camera']}")
        box.label(text=f"当前帧段: {start}–{end} · 当前帧: {current}{fps_text}")

    directory = _text(cache.get("input"))
    launch = cache.get("launch")
    if isinstance(launch, dict):
        if summary["filepath"]:
            matched = _matching_launch_shots(launch, summary["filepath"])
            if matched:
                pending = _pending_count(matched, cache.get("status"))
                if pending is None:
                    box.label(
                        text=f"当前工程请求 {len(matched)} 个镜头 · 状态未报告",
                        icon="INFO",
                    )
                else:
                    box.label(
                        text=f"当前工程请求 {len(matched)} 个镜头 · 未完成 {pending}",
                        icon="CHECKMARK" if pending == 0 else "INFO",
                    )
                ids = "、".join(_text(shot.get("id")) for shot in matched[:6])
                if len(matched) > 6:
                    ids += " …"
                box.label(text=f"匹配镜头: {ids}")
            else:
                box.label(text="当前工程不在所选批次的请求中", icon="INFO")
        else:
            box.label(text="保存 .blend 后可关联已提交请求", icon="INFO")
    elif directory and cache.get("root") is not None:
        box.label(text="未读取到 launch.json；无法展示已提交请求", icon="INFO")
    if not directory:
        box.label(text="未选择批次：以上仅为当前场景，不是已提交请求。", icon="INFO")


def _draw_controls(layout, manager):
    layout.prop(manager, _directory_property)
    layout.prop(manager, _jobs_root_property)
    row = layout.row(align=True)
    row.operator("rbw.refresh_status", text="刷新状态", icon="FILE_REFRESH")
    row.operator("rbw.scan_current_project", text="扫描当前工程", icon="VIEWZOOM")


def _draw_recovery(box, manager, state, cache):
    box.separator()
    box.label(text="CLI 恢复请求", icon="INFO")
    if state == "running":
        box.label(text="批次运行中；CLI 排他锁会拒绝重复恢复，拒绝原因写入输出。", icon="INFO")
        button_text = "请求 CLI 检查恢复"
    else:
        box.label(text="只请求 CLI 按冻结输入继续；状态仍以 status.json 为准。", icon="INFO")
        button_text = "请求 CLI 重试/恢复"
    box.prop(manager, _launcher_property)
    row = box.row(align=True)
    row.operator("rbw.resume_batch", text=button_text, icon="FILE_REFRESH")
    tail = _text(cache.get("cli_tail"))
    if tail:
        lowered = tail.casefold()
        tail_icon = "ERROR" if any(
            word in lowered for word in ("error", "conflict", "failed", "失败", "拒绝")
        ) else "INFO"
        box.label(text=f"最近 CLI: {tail}", icon=tail_icon)
    else:
        box.label(text="提交后由 CLI 判定；输出可在此查看。", icon="INFO")


def _draw_status_box(layout, context, status, launch, cache):
    manager = context.window_manager
    box = layout.box()
    state = _text(status.get("state"))
    review_error = _text(status.get("review_error"))
    if state == "failed" or review_error:
        icon = "ERROR"
    elif state == "completed":
        icon = "CHECKMARK"
    elif state == "running":
        icon = "PLAY"
    else:
        icon = "INFO"
    batch_id = _text(status.get("batch_id"))
    root = cache.get("root")
    if not batch_id and root is not None:
        batch_id = root.name
    box.label(text=f"{batch_id or '批次'} · {LABELS.get(state, state or '状态未记录')}", icon=icon)

    message = _text(status.get("message"))
    if message:
        _draw_text(box, message, max_lines=4, limit=240)
    elif state == "failed":
        box.label(text="status.json 未记录批次失败说明", icon="INFO")
    progress = _batch_progress_text(status, launch)
    if progress:
        box.label(text=progress, icon="INFO")
    updated = status.get("updated_unix")
    if isinstance(updated, (int, float)) and not isinstance(updated, bool):
        try:
            rendered = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated))
        except (OSError, OverflowError, ValueError):
            rendered = ""
        if rendered:
            box.label(text=f"状态更新: {rendered}")
    resume_count = status.get("resume_count")
    if type(resume_count) is int and resume_count > 0:
        box.label(text=f"恢复次数: {resume_count}")
    if status.get("cancellation_requested"):
        box.label(text="已请求取消；CLI 会保留通过核验的帧", icon="INFO")

    review = status.get("review")
    if isinstance(review, str) and review.strip():
        operator = box.operator("rbw.open_artifact", text="打开审片视频", icon="PLAY")
        operator.artifact = review
    elif review is not None and _text(review):
        box.label(text="review 字段不是路径，无法打开", icon="INFO")
    if review_error:
        box.label(text="审片失败", icon="ERROR")
        _draw_text(box, review_error, max_lines=6, limit=260)

    warnings = list(status.get("panel_warnings") or [])
    if isinstance(launch, dict):
        warnings.extend(launch.get("panel_warnings") or [])
    _draw_warnings(box, warnings)
    if state in RECOVERABLE_STATES:
        _draw_recovery(box, manager, state, cache)
    row = box.row(align=True)
    row.operator("rbw.open_status", text="状态文件", icon="INFO")
    if cache.get("cli_log") is not None:
        row.operator("rbw.open_cli_log", text="CLI 输出", icon="TEXT")


def _draw_shot(layout, launch_shot, status_shot, summary):
    box = layout.box()
    has_status = isinstance(status_shot, dict)
    state = _text(status_shot.get("state")) if has_status else ""
    ident = _text((launch_shot or {}).get("id") or (status_shot or {}).get("id")) or "?"
    completed, total = _progress_values(status_shot, launch_shot)
    if has_status:
        state_text = LABELS.get(state, state or "状态未记录")
        header = f"{ident} · {state_text} · {_shot_progress_text(completed, total)}"
        if state == "failed":
            icon = "ERROR"
        elif state == "completed":
            icon = "CHECKMARK"
        elif state == "running":
            icon = "PLAY"
        else:
            icon = "INFO"
    else:
        request = f"请求 {total} 帧" if total else "请求帧段未记录"
        header = f"{ident} · 状态未报告 · {request}"
        icon = "INFO"
    box.label(text=header, icon=icon)

    current_key = _project_key(summary.get("filepath"))
    if isinstance(launch_shot, dict):
        project = _name(launch_shot.get("project"))
        project_name = _path_name(project) if project else ""
        if project is None:
            box.label(text="请求项目路径缺失，无法关联当前工程", icon="ERROR")
        elif current_key is None:
            box.label(text=f"请求项目: {project_name or project}（当前工程未保存）", icon="INFO")
        elif _project_key(project) == current_key:
            box.label(text=f"当前工程 · {project_name}", icon="INFO")
        else:
            box.label(text=f"来自其他项目 · {project_name or project}", icon="INFO")
        scene = _name(launch_shot.get("scene"))
        view_layer = _name(launch_shot.get("view_layer"))
        box.label(text=f"请求场景: {scene or '未记录'} · 视图层: {view_layer or '未记录'}")
        camera = _name(launch_shot.get("camera"))
        box.label(text=f"请求相机: {camera or '未记录'} · 帧段: {_frame_range_text(launch_shot)}")
    elif current_key is not None:
        box.label(text="status.json 无项目路径，不能确认是否属于当前工程", icon="INFO")

    if has_status:
        actual = []
        camera = _name(status_shot.get("camera"))
        engine = _name(status_shot.get("engine"))
        if camera:
            actual.append(f"相机 {camera}")
        if engine:
            actual.append(engine)
        frames = status_shot.get("frames")
        if (isinstance(frames, list) and len(frames) == 2
                and all(type(value) is int for value in frames)):
            actual.append(f"帧 {frames[0]}–{frames[1]}")
        if actual:
            box.label(text="CLI 实际: " + " · ".join(actual))

        message = _text(status_shot.get("message"))
        if message and not (state == "completed" and message == "已完成"):
            if state == "failed":
                box.label(text="失败原因", icon="ERROR")
            elif state == "paused":
                box.label(text="暂停原因", icon="INFO")
            else:
                box.label(text="状态说明", icon="INFO")
            _draw_text(box, message, max_lines=6, limit=260)
        elif state == "failed":
            box.label(text="status.json 未记录失败原因", icon="INFO")

        row = box.row(align=True)
        shown = False
        for key, caption in (("preview", "预览"), ("final", "终稿")):
            value = status_shot.get(key)
            if isinstance(value, str) and value.strip():
                operator = row.operator("rbw.open_artifact", text=caption, icon="PLAY")
                operator.artifact = value
                shown = True
            elif value is not None and _text(value):
                box.label(text=f"{caption}路径字段无效", icon="INFO")
        if not shown and state == "completed":
            box.label(text="状态标记完成，但未记录可打开的预览/终稿产物", icon="ERROR")
    else:
        box.label(text="尚未在此批次的 status.json 中报告", icon="INFO")


def _draw_batch(layout, context, cache):
    status = cache.get("status")
    launch = cache.get("launch")
    status_error = cache.get("status_error")
    launch_error = cache.get("launch_error")

    if status_error:
        box = layout.box()
        box.label(text="批次状态不可用", icon="ERROR")
        _draw_text(box, status_error, max_lines=4, limit=240)
        if cache.get("root") is not None:
            box.operator("rbw.open_status", text="状态文件", icon="INFO")
    if isinstance(status, dict):
        _draw_status_box(layout, context, status, launch, cache)
    if launch_error:
        box = layout.box()
        box.label(text="已提交请求", icon="INFO")
        _draw_text(box, launch_error, max_lines=3, limit=240)
    elif isinstance(launch, dict) and not isinstance(status, dict):
        warnings = launch.get("panel_warnings") or []
        if warnings:
            box = layout.box()
            box.label(text="请求记录提示", icon="INFO")
            _draw_warnings(box, warnings)

    rows = _merged_shots(
        status if isinstance(status, dict) else None,
        launch if isinstance(launch, dict) else None,
    )
    if rows:
        layout.separator()
        layout.label(text="请求镜头与进度", icon="INFO")
        summary = _current_project_summary(context)
        for launch_shot, status_shot in rows:
            _draw_shot(layout, launch_shot, status_shot, summary)


class RBW_OT_refresh(bpy.types.Operator):
    bl_idname = "rbw.refresh_status"
    bl_label = "刷新状态"
    bl_description = "读取所选批次的 status.json 与 launch.json"

    def execute(self, context):
        value = getattr(context.window_manager, _directory_property, "")
        if not _text(value):
            self.report({"WARNING"}, "请先选择批次目录")
            return {"CANCELLED"}
        try:
            refresh(value)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if _cached.get("status_error"):
            self.report({"WARNING"}, _text(_cached["status_error"])[:200])
        elif _cached.get("launch_error"):
            self.report({"WARNING"}, _text(_cached["launch_error"])[:200])
        else:
            self.report({"INFO"}, "已读取批次状态与请求镜头")
        return {"FINISHED"}


class RBW_OT_open_artifact(bpy.types.Operator):
    bl_idname = "rbw.open_artifact"
    bl_label = "打开视频"

    artifact: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        try:
            path = resolve_batch_artifact(
                getattr(context.window_manager, _directory_property),
                self.artifact,
            )
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        try:
            return bpy.ops.wm.path_open(filepath=str(path))
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}


class RBW_OT_open_status(bpy.types.Operator):
    bl_idname = "rbw.open_status"
    bl_label = "打开 status.json"
    bl_description = "查看完整批次状态与失败原因"

    def execute(self, context):
        try:
            root = batch_directory(getattr(context.window_manager, _directory_property))
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        path = root / "status.json"
        if not path.is_file():
            self.report({"ERROR"}, "此批次没有 status.json")
            return {"CANCELLED"}
        if path.is_symlink():
            self.report({"ERROR"}, "status.json 不能是符号链接")
            return {"CANCELLED"}
        try:
            return bpy.ops.wm.path_open(filepath=str(path))
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}


class RBW_OT_open_cli_log(bpy.types.Operator):
    bl_idname = "rbw.open_cli_log"
    bl_label = "查看 CLI 输出"
    bl_description = "打开面板写入的 PowerShell/CLI 请求输出"

    def execute(self, context):
        try:
            root = batch_directory(getattr(context.window_manager, _directory_property))
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        path = root / CLI_LOG_NAME
        if not path.is_file():
            self.report({"INFO"}, "尚无 CLI 输出；请先请求 CLI 恢复")
            return {"CANCELLED"}
        if path.is_symlink():
            self.report({"ERROR"}, "CLI 输出不能是符号链接")
            return {"CANCELLED"}
        try:
            return bpy.ops.wm.path_open(filepath=str(path))
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}


class RBW_OT_resume(bpy.types.Operator):
    bl_idname = "rbw.resume_batch"
    bl_label = "请求 CLI 恢复"
    bl_description = (
        "通过所选 PowerShell 启动器请求 CLI 恢复批次；"
        "运行中由 CLI 排他锁决定是否拒绝"
    )

    def execute(self, context):
        manager = context.window_manager
        try:
            root = batch_directory(getattr(manager, _directory_property))
            status = read_batch_status(root)
            state = _text(status.get("state"))
            if state not in RECOVERABLE_STATES:
                raise ValueError(
                    f"当前批次状态为 {LABELS.get(state, state or '未知')}，CLI 不能从该状态恢复"
                )
            launcher_text = _text(getattr(manager, _launcher_property, ""))
            if not launcher_text:
                raise ValueError("选择与此批次配套的 blenderctl PowerShell 启动器")
            launcher = Path(bpy.path.abspath(launcher_text)).resolve()
            if not launcher.is_file() or launcher.suffix.casefold() != ".ps1":
                raise ValueError("选择与此批次配套的 blenderctl PowerShell 启动器")
            jobs_root = _effective_jobs_root(manager, root)
            if jobs_root is None:
                raise ValueError("无法推断 Jobs 根目录，请填写 Jobs 根目录")
            expected = jobs_root / "b" / root.name
            if os.path.normcase(str(expected.resolve())) != os.path.normcase(str(root.resolve())):
                raise ValueError("Jobs 根目录与所选批次不匹配")
            pwsh = shutil.which("pwsh")
            if pwsh is None:
                raise ValueError("找不到 PowerShell 7（pwsh）")
            log_path = root / CLI_LOG_NAME
            if log_path.is_symlink():
                raise ValueError("CLI 输出不能是符号链接")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("ab") as log:
                log.write(f"\n=== {stamp} · render batch resume {root.name} ===\n".encode("utf-8"))
                log.flush()
                argv = [
                    pwsh,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(launcher),
                    "--blender",
                    bpy.app.binary_path,
                    "--jobs-dir",
                    str(jobs_root),
                    "--async",
                    "render",
                    "batch",
                    "resume",
                    root.name,
                ]
                subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                )
            if _cached.get("root") == root:
                _cached["cli_tail"] = ""
            _redraw_views()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if state == "running":
            self.report({"INFO"}, "已提交 CLI 恢复检查；若被锁拒绝，请查看 CLI 输出")
        else:
            self.report({"INFO"}, "已提交 CLI 恢复/重试请求；结果以 CLI 输出和 status.json 为准")
        return {"FINISHED"}


class RBW_OT_scan_current_project(bpy.types.Operator):
    bl_idname = "rbw.scan_current_project"
    bl_label = "扫描当前工程"
    bl_description = "仅点击时扫描 Jobs 根目录下 b/*，切换到包含当前工程的最近批次"

    @classmethod
    def poll(cls, context):
        return bool(_text(getattr(bpy.data, "filepath", "")))

    def execute(self, context):
        manager = context.window_manager
        filepath = _text(getattr(bpy.data, "filepath", ""))
        if not filepath:
            self.report({"ERROR"}, "当前工程尚未保存")
            return {"CANCELLED"}
        batch_root = None
        try:
            batch_root = batch_directory(getattr(manager, _directory_property, ""))
        except (OSError, ValueError):
            batch_root = None
        try:
            jobs_root = _effective_jobs_root(manager, batch_root)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if jobs_root is None:
            self.report({"ERROR"}, "请填写 Jobs 根目录（批次目录不在 jobs/b 下）")
            return {"CANCELLED"}
        try:
            result = find_latest_project_batch(jobs_root, filepath)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if result["path"] is None:
            suffix = "；扫描范围已达到上限" if result["truncated"] else ""
            self.report(
                {"WARNING"},
                f"未找到包含当前工程的批次（检查了最新 {result['parsed']} 个 launch.json）{suffix}",
            )
            return {"CANCELLED"}
        if not _text(getattr(manager, _jobs_root_property, "")):
            setattr(manager, _jobs_root_property, str(jobs_root))
        setattr(manager, _directory_property, str(result["path"]))
        refresh(str(result["path"]))
        modified = result.get("modified_unix")
        stamp = ""
        if isinstance(modified, (int, float)) and not isinstance(modified, bool):
            try:
                stamp = " · " + time.strftime("%Y-%m-%d %H:%M", time.localtime(modified))
            except (OSError, OverflowError, ValueError):
                stamp = ""
        self.report(
            {"INFO"},
            f"已切换到 {result['path'].name}{stamp}（检查 {result['parsed']} 个最近 launch.json）",
        )
        return {"FINISHED"}


class RBW_PT_batch_status(bpy.types.Panel):
    bl_idname = "RBW_PT_batch_status"
    bl_label = "渲染批次"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "渲染批次"

    def draw(self, context):
        try:
            self._draw(context)
        except Exception as error:
            self.layout.label(text=f"面板绘制失败: {str(error)[:160]}", icon="ERROR")

    def _draw(self, context):
        layout = self.layout
        manager = context.window_manager
        directory = _text(getattr(manager, _directory_property, ""))
        ready = _cached.get("input") == directory
        cache = _cached if ready else _empty_cache(directory)
        _draw_current_project(layout, context, cache)
        layout.separator()
        _draw_controls(layout, manager)
        if not directory:
            return
        if not ready:
            layout.label(text="批次目录已更改；点击“刷新状态”读取。", icon="INFO")
            return
        _draw_batch(layout, context, cache)


_CLASSES = (
    RBW_OT_refresh,
    RBW_OT_open_artifact,
    RBW_OT_open_status,
    RBW_OT_open_cli_log,
    RBW_OT_resume,
    RBW_OT_scan_current_project,
    RBW_PT_batch_status,
)


def register():
    setattr(
        bpy.types.WindowManager,
        _directory_property,
        StringProperty(
            name="批次目录",
            description="CLI 回执中的 jobs/b/<批次编号> 目录",
            subtype="DIR_PATH",
            options={"SKIP_SAVE"},
        ),
    )
    setattr(
        bpy.types.WindowManager,
        _launcher_property,
        StringProperty(
            name="PowerShell 启动器",
            description="配套的 blenderctl PowerShell 启动器（.ps1）",
            subtype="FILE_PATH",
            options={"SKIP_SAVE"},
        ),
    )
    setattr(
        bpy.types.WindowManager,
        _jobs_root_property,
        StringProperty(
            name="Jobs 根目录（可选）",
            description="包含 b 子目录的 blenderctl jobs 目录；留空时从所选批次推断",
            subtype="DIR_PATH",
            options={"SKIP_SAVE"},
        ),
    )
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.background and not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.5, persistent=True)


def unregister():
    global _cached
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    for name in (_directory_property, _launcher_property, _jobs_root_property):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)
    _launch_cache.clear()
    _cached = _empty_cache()
