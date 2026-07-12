"""
批量上传当前目录（sample_documents/）中的样例文档到 KQuery Pro 系统

通过 REST API 将文档上传到向量数据库，支持 RAG 处理和知识图谱构建。

用法:
    python upload_sample_documents.py [--base-url URL] [--collection NAME] [--build-kg]

示例:
    # 默认上传到 localhost:8000，集合名 sample_documents
    python upload_sample_documents.py

    # 指定服务地址和集合名
    python upload_sample_documents.py --base-url http://192.168.1.100:8000 --collection my_docs

    # 上传并构建知识图谱
    python upload_sample_documents.py --build-kg
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

# 当前目录即为样例文档目录
SAMPLE_DIR = Path(__file__).resolve().parent

# 默认 API 基础 URL
DEFAULT_BASE_URL = "http://localhost:8000"
# 默认集合名称
DEFAULT_COLLECTION = "sample_documents"


def get_supported_file_types(base_url: str) -> set:
    """获取服务端支持的文件类型"""
    try:
        resp = requests.get(f"{base_url}/api/v1/kquery/supported-file-types", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return set(data.get("allowed_extensions", []))
    except Exception as e:
        print(f"  ⚠ 获取支持文件类型失败: {e}")
    return set()


def upload_document(
    base_url: str,
    file_path: Path,
    collection_name: str,
    build_kg: bool = False,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> dict:
    """上传单个文档到系统"""
    endpoint = (
        f"{base_url}/api/v1/kquery/documents/process-and-build"
        if build_kg
        else f"{base_url}/api/v1/kquery/documents/process"
    )

    params = {
        "collection_name": collection_name,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }

    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, _guess_mime(file_path.suffix))}
        resp = requests.post(endpoint, params=params, files=files, timeout=600)

    if resp.status_code == 200:
        return resp.json()
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")


def _guess_mime(suffix: str) -> str:
    """根据扩展名猜测 MIME 类型"""
    mapping = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".ppt": "application/vnd.ms-powerpoint",
        ".csv": "text/csv",
        ".xml": "application/xml",
        ".json": "application/json",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")


def main():
    parser = argparse.ArgumentParser(
        description="批量上传样例文档到 KQuery Pro 系统"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API 基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"向量集合名称 (默认: {DEFAULT_COLLECTION})",
    )
    parser.add_argument(
        "--build-kg",
        action="store_true",
        help="同时构建知识图谱",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="文本块大小 (默认: 500)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=50,
        help="块重叠大小 (默认: 50)",
    )
    args = parser.parse_args()

    # 检查样例文档目录
    if not SAMPLE_DIR.exists():
        print(f"❌ 样例文档目录不存在: {SAMPLE_DIR}")
        sys.exit(1)

    # 获取支持的文件类型
    print(f"🔍 检查服务状态: {args.base_url}")
    try:
        health = requests.get(f"{args.base_url}/health", timeout=10)
        if health.status_code != 200:
            print(f"❌ 服务未就绪，请确保后端服务已启动")
            sys.exit(1)
        print(f"✅ 服务状态正常 (v{health.json().get('version', '?')})")
    except requests.ConnectionError:
        print(f"❌ 无法连接到 {args.base_url}，请确保后端服务已启动")
        sys.exit(1)

    supported = get_supported_file_types(args.base_url)
    if supported:
        print(f"📋 服务端支持的文件类型: {', '.join(sorted(supported))}")

    # 收集所有文档文件
    all_files = sorted(SAMPLE_DIR.iterdir())
    doc_files = []
    skipped = []

    for f in all_files:
        if f.is_file() and f.name != os.path.basename(__file__):
            ext = f.suffix.lower()
            if ext in supported or not supported:
                doc_files.append(f)
            else:
                skipped.append(f)

    if not doc_files:
        print("❌ 没有找到可上传的文档文件")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"📤 开始上传 {len(doc_files)} 个文档到集合 '{args.collection}'")
    if args.build_kg:
        print(f"🧠 知识图谱构建: 已启用")
    print(f"{'='*60}\n")

    # 逐个上传文档
    success_count = 0
    fail_count = 0
    total_start = time.time()

    for i, file_path in enumerate(doc_files, 1):
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"[{i}/{len(doc_files)}] {file_path.name} ({size_mb:.1f} MB)...", end=" ")

        start = time.time()
        try:
            result = upload_document(
                base_url=args.base_url,
                file_path=file_path,
                collection_name=args.collection,
                build_kg=args.build_kg,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            elapsed = time.time() - start

            if result.get("success", False):
                chunk_count = result.get("chunk_count", 0) or result.get("rag", {}).get("chunk_count", 0)
                print(f"✅ {chunk_count} 个文本块 ({elapsed:.1f}s)")
                success_count += 1
            else:
                error = result.get("error_message", "未知错误")
                print(f"❌ {error}")
                fail_count += 1

        except Exception as e:
            elapsed = time.time() - start
            print(f"❌ {e} ({elapsed:.1f}s)")
            fail_count += 1

    # 输出汇总
    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"📊 上传完成汇总")
    print(f"   集合名称: {args.collection}")
    print(f"   总文件数: {len(doc_files)}")
    print(f"   成功: {success_count}")
    print(f"   失败: {fail_count}")
    print(f"   总耗时: {total_elapsed:.1f}s")
    print(f"{'='*60}")

    if skipped:
        print(f"\n⚠ 跳过的文件（不支持的类型）:")
        for f in skipped:
            print(f"   - {f.name}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
