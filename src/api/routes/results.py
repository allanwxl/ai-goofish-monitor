"""
结果文件管理路由
"""
import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from enum import Enum

from pydantic import BaseModel
from urllib.parse import quote

from src.services.price_history_service import build_price_history_insights
from src.services.item_status_service import build_sold_status_insights
from src.services.item_status_task_runner import (
    is_manual_status_check_enabled,
    run_standalone_status_check,
)
from src.services.result_export_service import build_results_csv
from src.services.result_file_service import (
    enrich_records_with_price_insight,
    validate_result_filename,
)
from src.services.result_storage_service import (
    build_result_ndjson,
    delete_result_file_records,
    list_result_filenames,
    load_all_result_records,
    load_result_blacklist_keywords,
    load_visible_result_item_ids,
    query_result_records,
    result_file_exists,
    save_result_blacklist_keywords,
    update_item_status,
)


router = APIRouter(prefix="/api/results", tags=["results"])

DEFAULT_EXPORT_FILENAME = "export.csv"

# 手动状态复核的后台任务句柄（防止被垃圾回收）
_STATUS_CHECK_TASKS: set[asyncio.Task] = set()


def _build_download_headers(export_name: str) -> dict[str, str]:
    ascii_name = export_name.encode("ascii", "ignore").decode("ascii")
    if ascii_name != export_name or not ascii_name:
        ascii_name = DEFAULT_EXPORT_FILENAME
    encoded_name = quote(export_name, safe="")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }


@router.get("/files")
async def get_result_files():
    """获取所有结果文件列表"""
    return {"files": await list_result_filenames()}


@router.get("/files/{filename:path}")
async def download_result_file(filename: str):
    """下载指定的结果文件"""
    if ".." in filename or filename.startswith("/"):
        return {"error": "非法的文件路径"}
    if not filename.endswith(".jsonl") or not await result_file_exists(filename):
        return {"error": "文件不存在"}
    return Response(
        content=await build_result_ndjson(filename),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/files/{filename:path}")
async def delete_result_file(filename: str):
    """删除指定的结果文件"""
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not filename.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="只能删除 .jsonl 文件")
    deleted_rows = await delete_result_file_records(filename)
    if deleted_rows <= 0:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"message": f"文件 {filename} 已成功删除"}


@router.get("/{filename}")
async def get_result_file_content(
    filename: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    recommended_only: bool = Query(False),  # 兼容旧参数，等价于 ai_recommended_only
    ai_recommended_only: bool = Query(False),
    keyword_recommended_only: bool = Query(False),
    include_hidden: bool = Query(False),
    listing_status: list[str] = Query(default=[]),
    sort_by: str = Query("crawl_time"),
    sort_order: str = Query("desc"),
):
    """读取指定的 .jsonl 文件内容，支持分页、筛选和排序"""
    if ai_recommended_only and keyword_recommended_only:
        raise HTTPException(status_code=400, detail="AI推荐筛选与关键词推荐筛选不能同时开启。")

    if recommended_only and not ai_recommended_only and not keyword_recommended_only:
        ai_recommended_only = True

    try:
        validate_result_filename(filename)
        total_items, items = await query_result_records(
            filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
            include_hidden=include_hidden,
            listing_status=listing_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取结果文件时出错: {exc}")
    if total_items <= 0 and not await result_file_exists(filename):
        raise HTTPException(status_code=404, detail="结果文件未找到")
    paginated_results = enrich_records_with_price_insight(items, filename)

    return {
        "total_items": total_items,
        "page": page,
        "limit": limit,
        "items": paginated_results
    }


@router.get("/{filename}/insights")
async def get_result_file_insights(filename: str):
    try:
        validate_result_filename(filename)
        keyword = filename.replace("_full_data.jsonl", "")
        visible_item_ids = load_visible_result_item_ids(filename)
        insights = build_price_history_insights(keyword, visible_item_ids=visible_item_ids)
        insights.update(build_sold_status_insights(filename))
        return insights
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{filename}/export")
async def export_result_file_content(
    filename: str,
    recommended_only: bool = Query(False),
    ai_recommended_only: bool = Query(False),
    keyword_recommended_only: bool = Query(False),
    include_hidden: bool = Query(False),
    listing_status: list[str] = Query(default=[]),
    sort_by: str = Query("crawl_time"),
    sort_order: str = Query("desc"),
):
    if ai_recommended_only and keyword_recommended_only:
        raise HTTPException(status_code=400, detail="AI推荐筛选与关键词推荐筛选不能同时开启。")
    if recommended_only and not ai_recommended_only and not keyword_recommended_only:
        ai_recommended_only = True

    try:
        validate_result_filename(filename)
        results = await load_all_result_records(
            filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            listing_status=listing_status,
        )
        csv_text = build_results_csv(
            enrich_records_with_price_insight(results, filename)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导出结果文件时出错: {exc}")
    if not results and not await result_file_exists(filename):
        raise HTTPException(status_code=404, detail="结果文件未找到")

    export_name = filename.replace(".jsonl", ".csv")
    headers = _build_download_headers(export_name)
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)


class ItemStatus(str, Enum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    EXPIRED = "expired"


class UpdateStatusRequest(BaseModel):
    status: ItemStatus


class BlacklistRulesRequest(BaseModel):
    keywords: list[str]


@router.patch("/{filename}/items/{item_id}/status")
async def patch_item_status(filename: str, item_id: str, body: UpdateStatusRequest):
    """更新指定商品的状态（active/hidden/expired）"""
    try:
        validate_result_filename(filename)
        updated = await update_item_status(filename, item_id, body.status.value)
        if not updated:
            raise HTTPException(status_code=404, detail="商品未找到")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": "状态已更新", "status": body.status.value}


@router.post("/{filename}/status-check")
async def trigger_status_check(filename: str):
    """手动触发一次商品状态复核（已售 / 已下架）。

    默认关闭：需要显式设置 ``ITEM_STATUS_MANUAL_CHECK_ENABLED=true``。
    这个接口会在 Web 服务进程里新开一个浏览器并向闲鱼发请求，
    而后端 API 目前没有鉴权中间件，请在受信网络内使用。
    """
    if not is_manual_status_check_enabled():
        raise HTTPException(
            status_code=403,
            detail="手动状态复核未启用，如需使用请设置 ITEM_STATUS_MANUAL_CHECK_ENABLED=true。",
        )
    try:
        validate_result_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    keyword = filename.replace("_full_data.jsonl", "")
    task = asyncio.create_task(
        run_standalone_status_check(
            keyword=keyword,
            result_filename=filename,
        )
    )
    # 任务句柄要留一份强引用，否则可能被垃圾回收掉
    _STATUS_CHECK_TASKS.add(task)
    task.add_done_callback(_STATUS_CHECK_TASKS.discard)

    return {
        "message": "状态复核已启动，完成后刷新结果页即可看到最新状态。",
        "filename": filename,
        "keyword": keyword,
    }


@router.get("/{filename}/blacklist-rules")
async def get_result_blacklist_rules(filename: str):
    try:
        validate_result_filename(filename)
        keywords = await load_result_blacklist_keywords(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"keywords": keywords}


@router.put("/{filename}/blacklist-rules")
async def put_result_blacklist_rules(filename: str, body: BlacklistRulesRequest):
    try:
        validate_result_filename(filename)
        keywords = await save_result_blacklist_keywords(filename, body.keywords)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": "黑名单规则已更新", "keywords": keywords}
