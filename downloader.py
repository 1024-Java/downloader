# M3U8文件下载脚本 v2.0 

import os
import ssl
import shutil
import asyncio
import aiohttp
import aiofiles
import requests
import warnings
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from urllib.parse import urljoin

# 禁用SSL警告
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
requests.packages.urllib3.disable_warnings()

# 忽略证书验证
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

class Downloader:
    def __init__(self, url="", domain=""):
        self.url = url
        self.domain = domain
        self.video = "Video.ts"
        self.cache = "cacheSegments"
        self.max_concurrent = 10
        self.M3U8 = "./m3u8.txt"
        self.ua = {"user-agent": "Mozilla/5.0 (Linux; Android 12; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36"}
        self.key = ""
        self.iv = ""
        self.status = False

    def load(self):
        """加载并解析M3U8文件"""
        os.makedirs(self.cache, exist_ok=True)
        
        # 获取M3U8内容
        if self.url:
            resp = requests.get(self.url, headers=self.ua, verify=False, timeout=10)
            if resp.status_code != 200:
                print(f"M3U8请求失败: {resp.status_code}")
                return []
            content = resp.text
            with open(self.M3U8, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"M3U8获取成功，并发数: {self.max_concurrent}")
        else:
            try:
                with open(self.M3U8, "r", encoding="utf-8") as f:
                    content = f.read()
                print("使用本地M3U8文件")
            except:
                print("无法读取M3U8文件")
                return []
        
        # 解析加密信息
        for line in content.splitlines():
            if line.startswith("#EXT-X-KEY"):
                print(f"发现加密: {line}")
                self.status = True
                parts = line.split(",")
                for part in parts:
                    if "URI=" in part:
                        self.key = part.split("=")[1].strip('"')
                        if not self.key.startswith("http"):
                            base = self.url.rsplit("/", 1)[0] + "/" if self.url else self.domain
                            self.key = urljoin(base, self.key)
                    if "IV=" in part:
                        self.iv = part.split("=")[1].strip('"')
                if not self.iv:
                    self.iv = "00000000000000000000000000000000"
                break
        
        # 解析URL
        urls = []
        base_url = self.url.rsplit("/", 1)[0] + "/" if self.url else self.domain or ""
        
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("http"):
                    urls.append(line)
                elif line.startswith("/"):
                    if self.url:
                        domain = self.url.split("/")[0] + "//" + self.url.split("/")[2]
                        urls.append(domain + line)
                    elif self.domain:
                        urls.append(self.domain + line)
                else:
                    urls.append(urljoin(base_url, line))
        
        if urls:
            print(f"解析到 {len(urls)} 个片段")
        return urls

    def splice(self):
        """合并视频片段"""
        files = sorted([f for f in os.listdir(self.cache) if f.endswith(".ts")],
                      key=lambda x: int(x.split(".")[0]))
        
        if len(files) < 5:
            print("片段数量不足")
            return False
        
        with open(self.video, "wb") as out:
            for f in files:
                with open(f"{self.cache}/{f}", "rb") as seg:
                    out.write(seg.read())
                print(f"合并: {f}")
        print("合并完成!")
        return True

    def run(self):
        """运行下载器"""
        async def downloader():
            urls = self.load()
            if not urls:
                print("无URL可下载")
                return
            
            connector = aiohttp.TCPConnector(limit=self.max_concurrent, ssl=ssl_context)
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=self.ua) as session:
                sem = asyncio.Semaphore(self.max_concurrent)
                
                async def download(url, idx):
                    async with sem:
                        for _ in range(3):
                            try:
                                async with session.get(url, ssl=ssl_context) as resp:
                                    if resp.status == 200:
                                        data = await resp.read()
                                        async with aiofiles.open(f"{self.cache}/{idx}.ts", "wb") as f:
                                            await f.write(data)
                                        print(f"✓ {idx}/{len(urls)}")
                                        return True
                            except Exception as e:
                                print(f"✗ {idx}: {e}")
                                await asyncio.sleep(1)
                        return False
                
                tasks = [download(url, i) for i, url in enumerate(urls)]
                results = await asyncio.gather(*tasks)
                print(f"下载完成: {sum(results)}/{len(urls)}")
            
            if self.splice() and self.status:
                print("正在解密...")
                self.decrypt()
            elif os.path.exists(self.video):
                os.rename(self.video, "decrypted_video.ts")
                print("已保存为 decrypted_video.ts")
        
        try:
            asyncio.run(downloader())
        except KeyboardInterrupt:
            print("\n用户中断")
        finally:
            if os.path.exists(self.cache):
                shutil.rmtree(self.cache)
                os.remove("m3u8.txt")
                print("清理缓存")

    def decrypt(self):
        """解密视频文件"""
        try:
            # 获取密钥
            print(f"获取密钥: {self.key}")
            if self.key.startswith("http"):
                resp = requests.get(self.key, headers=self.ua, verify=False, timeout=10)
                key_bytes = resp.content
                print(f"密钥下载成功，长度: {len(key_bytes)}")
            else:
                key_bytes = bytes.fromhex(self.key) if len(self.key) == 32 else self.key.encode()
            
            # 处理IV
            iv_str = self.iv.replace("0x", "") if self.iv.startswith("0x") else self.iv
            if len(iv_str) == 32:
                iv_bytes = bytes.fromhex(iv_str)
            else:
                iv_bytes = iv_str.encode()[:16] if iv_str else b'\x00' * 16
                if len(iv_bytes) < 16:
                    iv_bytes += b'\x00' * (16 - len(iv_bytes))
            
            print(f"密钥长度: {len(key_bytes)}, IV: {iv_bytes.hex()}")
            
            # 读取加密数据
            with open(self.video, "rb") as f:
                encrypted = f.read()
            
            # 调整密钥长度
            if len(key_bytes) not in [16, 24, 32]:
                key_bytes = (key_bytes + b'\x00' * 32)[:32]
            
            # 解密
            cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted) + decryptor.finalize()
            
            # 尝试移除填充
            try:
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16:
                    decrypted = decrypted[:-pad_len]
            except:
                pass
            
            # 保存解密文件
            with open("decrypted_video.ts", "wb") as f:
                f.write(decrypted)
            
            print("解密成功!")
            return True
            
        except Exception as e:
            print(f"解密失败: {e}")
            return False


if __name__ == '__main__':
    url = input("input url:")
    Downloader(url=url).run()
