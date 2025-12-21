#!/usr/bin/env python3
"""
代理性能对比测试脚本
对比 wsocks 和 v2ray 的性能
"""

import time
import requests
import statistics
from typing import Dict, List, Tuple
import argparse

# 测试 URL 列表
TEST_URLS = [
    "https://www.google.com",
    "https://www.youtube.com",
    # "https://www.github.com",
    # "https://duckduckgo.com",
]

# 下载测试文件（约 1MB）
DOWNLOAD_TEST_URL = "https://speed.cloudflare.com/__down?bytes=1000000"


def test_latency(proxy: str, url: str, timeout: int = 10) -> Tuple[float, bool]:
    """
    测试延迟
    返回：(延迟时间(ms), 是否成功)
    """
    try:
        start = time.time()
        print(proxy)
        response = requests.get(
            url,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            allow_redirects=True,
            headers={
                'UserAgent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'
            }
        )
        latency = (time.time() - start) * 1000  # 转换为毫秒
        success = response.status_code == 200
        return latency, success
    except Exception as e:
        raise
        return 0, False


def test_download_speed(proxy: str, url: str, timeout: int = 30) -> Tuple[float, bool]:
    """
    测试下载速度
    返回：(速度(MB/s), 是否成功)
    """
    try:
        start = time.time()
        response = requests.get(
            url,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            stream=True
        )

        total_size = 0
        for chunk in response.iter_content(chunk_size=8192):
            total_size += len(chunk)

        elapsed = time.time() - start
        speed_mbps = (total_size / 1024 / 1024) / elapsed  # MB/s
        return speed_mbps, True
    except Exception as e:
        print(f"  ❌ 错误: {str(e)[:50]}")
        return 0, False


def run_latency_tests(proxy_name: str, proxy: str, urls: List[str], rounds: int = 3) -> Dict:
    """运行延迟测试"""
    print(f"\n{'='*60}")
    print(f"🔍 测试 {proxy_name} - 延迟测试")
    print(f"{'='*60}")

    results = []

    for url in urls:
        print(f"\n测试 {url}...")
        latencies = []

        for i in range(rounds):
            latency, success = test_latency(proxy, url)
            if success:
                latencies.append(latency)
                print(f"  第 {i+1} 次: {latency:.0f} ms")
            else:
                print(f"  第 {i+1} 次: 失败")

        if latencies:
            avg_latency = statistics.mean(latencies)
            results.append(avg_latency)
            print(f"  📊 平均延迟: {avg_latency:.0f} ms")
        else:
            print(f"  ❌ 所有请求都失败")

    return {
        "latencies": results,
        "avg_latency": statistics.mean(results) if results else 0,
        "success_count": len(results),
        "total_count": len(urls)
    }


def run_download_tests(proxy_name: str, proxy: str, url: str, rounds: int = 3) -> Dict:
    """运行下载速度测试"""
    print(f"\n{'='*60}")
    print(f"🚀 测试 {proxy_name} - 下载速度测试")
    print(f"{'='*60}")

    speeds = []

    for i in range(rounds):
        print(f"\n第 {i+1} 次下载测试...")
        speed, success = test_download_speed(proxy, url)
        if success:
            speeds.append(speed)
            print(f"  ✅ 下载速度: {speed:.2f} MB/s")
        else:
            print(f"  ❌ 下载失败")

    return {
        "speeds": speeds,
        "avg_speed": statistics.mean(speeds) if speeds else 0,
        "max_speed": max(speeds) if speeds else 0,
        "success_count": len(speeds),
        "total_count": rounds
    }


def print_comparison(wsocks_results: Dict, v2ray_results: Dict, no_latency: bool, no_download: bool):
    """打印对比结果"""
    print(f"\n{'='*60}")
    print("📊 性能对比总结")
    print(f"{'='*60}\n")
    if not no_latency:
        # 延迟对比
        print("🔍 延迟对比:")
        print(f"  wsocks:  {wsocks_results['latency']['avg_latency']:.0f} ms "
              f"({wsocks_results['latency']['success_count']}/{wsocks_results['latency']['total_count']} 成功)")
        print(f"  v2ray:   {v2ray_results['latency']['avg_latency']:.0f} ms "
              f"({v2ray_results['latency']['success_count']}/{v2ray_results['latency']['total_count']} 成功)")

        if wsocks_results['latency']['avg_latency'] > 0 and v2ray_results['latency']['avg_latency'] > 0:
            diff = wsocks_results['latency']['avg_latency'] - v2ray_results['latency']['avg_latency']
            if abs(diff) < 10:
                print(f"  结果: 延迟相近 (差距 {abs(diff):.0f} ms)")
            elif diff > 0:
                print(f"  结果: v2ray 延迟更低 (快 {abs(diff):.0f} ms)")
            else:
                print(f"  结果: wsocks 延迟更低 (快 {abs(diff):.0f} ms)")

    # 下载速度对比
    if not no_download:
        print("\n🚀 下载速度对比:")
        print(f"  wsocks:  {wsocks_results['download']['avg_speed']:.2f} MB/s "
              f"(最高 {wsocks_results['download']['max_speed']:.2f} MB/s)")
        print(f"  v2ray:   {v2ray_results['download']['avg_speed']:.2f} MB/s "
              f"(最高 {v2ray_results['download']['max_speed']:.2f} MB/s)")

        if wsocks_results['download']['avg_speed'] > 0 and v2ray_results['download']['avg_speed'] > 0:
            ratio = wsocks_results['download']['avg_speed'] / v2ray_results['download']['avg_speed']
            if 0.9 < ratio < 1.1:
                print(f"  结果: 速度相近")
            elif ratio > 1.1:
                print(f"  结果: wsocks 速度更快 ({ratio:.1f}x)")
            else:
                print(f"  结果: v2ray 速度更快 ({1/ratio:.1f}x)")

    print(f"\n{'='*60}")


def main():
    parser = argparse.ArgumentParser(description='代理性能对比测试')
    parser.add_argument('--wsocks-port', type=int, default=1089, help='wsocks 端口 (默认: 1089)')
    parser.add_argument('--v2ray-port', type=int, default=4086, help='v2ray 端口 (默认: 4086)')
    parser.add_argument('--latency-rounds', type=int, default=30, help='延迟测试轮数 (默认: 30)')
    parser.add_argument('--download-rounds', type=int, default=10, help='下载测试轮数 (默认: 10)')
    parser.add_argument('--no-latency', action='store_true', help='跳过延迟测试')
    parser.add_argument('--no-download', action='store_true', help='跳过下载测试')

    args = parser.parse_args()

    wsocks_proxy = f"socks5h://127.0.0.1:{args.wsocks_port}"
    v2ray_proxy = f"socks5h://127.0.0.1:{args.v2ray_port}"

    print("🔬 代理性能对比测试")
    print(f"wsocks: {wsocks_proxy}")
    print(f"v2ray:  {v2ray_proxy}")

    wsocks_results = {}
    v2ray_results = {}

    # 延迟测试
    if not args.no_latency:
        wsocks_results['latency'] = run_latency_tests("wsocks", wsocks_proxy, TEST_URLS, args.latency_rounds)
        v2ray_results['latency'] = run_latency_tests("v2ray", v2ray_proxy, TEST_URLS, args.latency_rounds)

    # 下载速度测试
    if not args.no_download:
        wsocks_results['download'] = run_download_tests("wsocks", wsocks_proxy, DOWNLOAD_TEST_URL, args.download_rounds)
        v2ray_results['download'] = run_download_tests("v2ray", v2ray_proxy, DOWNLOAD_TEST_URL, args.download_rounds)

    # 打印对比结果
    if wsocks_results and v2ray_results:
        print_comparison(wsocks_results, v2ray_results, args.no_latency, args.no_download)


if __name__ == "__main__":
    main()
