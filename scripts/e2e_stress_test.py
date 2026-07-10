import argparse
import asyncio
import os
import random
import sys
import time

# 把当前目录加入 sys.path 才能引用 trawler
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trawler.crawl_url import crawl_url

# 模拟真实的流量特征
DOMAINS = [
    "example.com",
    "test.com",
    "demo.org",
    "mock.net",
    "crawler-test.local"
]

# 用于记录各个结果的计数器
stats = {
    "success": 0,
    "error": 0,
    "timeout": 0,
    "rate-limit": 0,
    "blocked-bot": 0,
    "blocked-ssrf": 0,
    "blocked-ssrf-redirect": 0,
    "session-expired": 0,
    "empty-content": 0,
    "all-fetchers-failed": 0,
    "unknown": 0
}

async def worker(worker_id: int, num_requests: int, sem: asyncio.Semaphore):
    """
    单个 worker，连续发起请求
    """
    for i in range(num_requests):
        domain = random.choice(DOMAINS)
        url = f"https://{domain}/path/to/page_{worker_id}_{i}"
        
        # 加上随机的小抖动，避免所有请求完全同步
        await asyncio.sleep(random.uniform(0.01, 0.1))
        
        try:
            async with sem:
                # 真实走 crawl_url (会经过所有的梯队降级、速率限制锁等)
                # 使用 timeout=10 避免整个测试无限制拖长
                result = await crawl_url(url, timeout=10)
                
                if result.startswith("__TRAWLER_OK__:"):
                    stats["success"] += 1
                elif result.startswith("__TRAWLER_ERROR__:"):
                    import json
                    try:
                        err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
                        etype = err_json.get("errorType", "unknown")
                        if etype in stats:
                            stats[etype] += 1
                        else:
                            stats["unknown"] += 1
                    except Exception:
                        stats["error"] += 1
                else:
                    stats["unknown"] += 1
        except Exception:
            stats["error"] += 1

async def main():
    parser = argparse.ArgumentParser(description="Trawler E2E Stress Test")
    parser.add_argument(
        "--workers",
        type=int,
        default=100,
        help="Number of concurrent workers (default: 100)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=10,
        help="Requests per worker (default: 10)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Max active requests in flight (default: 50)",
    )
    args = parser.parse_args()

    total_requests = args.workers * args.requests
    print(
        f"Starting stress test: {total_requests} total requests "
        f"({args.workers} workers x {args.requests} reqs) "
        f"with concurrency limit {args.concurrency}"
    )

    # 使用全局信号量控制真正的在途请求数，模拟连接池上限
    sem = asyncio.Semaphore(args.concurrency)
    
    start_time = time.time()
    
    tasks = [
        worker(w, args.requests, sem)
        for w in range(args.workers)
    ]
    
    await asyncio.gather(*tasks)
    
    elapsed = time.time() - start_time
    qps = total_requests / elapsed if elapsed > 0 else 0
    
    print("\n--- Test Completed ---")
    print(f"Total time: {elapsed:.2f} seconds")
    print(f"Throughput: {qps:.2f} req/s")
    print("\nResult Distribution:")
    for k, v in stats.items():
        if v > 0:
            print(f"  {k}: {v} ({(v/total_requests)*100:.1f}%)")

if __name__ == "__main__":
    # 使用 Windows 推荐的 ProactorEventLoop 确保高并发不出错
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
