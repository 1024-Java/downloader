# M3U8文件下载脚本
import os
import ssl
import shutil
import asyncio
import aiohttp
import aiofiles
import requests
import warnings
from urllib.parse import urljoin
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 禁用SSL警告
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
requests.packages.urllib3.disable_warnings()

# 忽略证书验证（全局SSL上下文）
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


class Downloader:
    """M3U8视频下载器（支持AES-128加密解密）"""

    def __init__(self, url="", domain=""):
        self.url = url                      # M3U8地址
        self.domain = domain                # 备用域名（当URL相对路径时使用）
        self.output_file = "video.ts"       # 合并后的视频文件（未加密de）
        self.cache_dir = "cacheSegments"    # 片段缓存目录
        self.max_concurrent = 30            # 最大并发下载数
        self.m3u8_file = "./m3u8.txt"       # 保存M3U8内容的临时文件
        self.headers = {                    # 请求头（模拟移动端）
            "user-agent": "Mozilla/5.0 (Linux; Android 12; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36"
        }
        self.key_uri = ""                  # 密钥URI（原始字符串）
        self.key_data = None               # 密钥字节数据（下载后）
        self.iv_str = ""                   # IV字符串（十六进制或0x格式）
        self.iv_bytes = None               # IV字节数据
        self.is_encrypted = False          # 是否为加密流

    def _fetch_m3u8_content(self):
        """下载M3U8文件内容，返回文本"""
        if not self.url:
            raise ValueError("M3U8 URL不能为空")
        resp = requests.get(self.url, headers=self.headers, verify=False, timeout=10)
        if resp.status_code != 200:
            raise Exception(f"M3U8请求失败，状态码: {resp.status_code}")
        content = resp.text
        # 保存原始内容以便调试
        with open(self.m3u8_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"M3U8获取成功，大小: {len(content)} 字节，并发数: {self.max_concurrent}")
        return content

    def _parse_encryption_info(self, lines):
        """解析M3U8中的加密信息（EXT-X-KEY）"""
        for line in lines:
            if line.startswith("#EXT-X-KEY"):
                print(f"检测到加密标记: {line}")
                self.is_encrypted = True
                parts = line.split(",")
                for part in parts:
                    part = part.strip()
                    if part.startswith("URI="):
                        self.key_uri = part.split("=", 1)[1].strip('"')
                        # 处理相对路径
                        if not self.key_uri.startswith("http"):
                            base = self.url.rsplit("/", 1)[0] + "/" if self.url else self.domain
                            self.key_uri = urljoin(base, self.key_uri)
                    if part.startswith("IV="):
                        iv_raw = part.split("=", 1)[1].strip('"')
                        self.iv_str = iv_raw.replace("0x", "") if iv_raw.startswith("0x") else iv_raw
                # 若未提供IV，则使用全0 IV（16字节）
                if not self.iv_str:
                    self.iv_str = "00" * 16
                print(f"密钥URI: {self.key_uri}, IV: {self.iv_str[:16]}...")
                break  # 通常只有一个加密标记，取第一个即可

    def _parse_segment_urls(self, lines, base_url):
        """解析M3U8中的TS片段URL列表"""
        urls = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http"):
                urls.append(line)
            elif line.startswith("/"):
                # 绝对路径，从URL中提取协议和域名
                if self.url:
                    domain = self.url.split("/")[0] + "//" + self.url.split("/")[2]
                    urls.append(domain + line)
                elif self.domain:
                    urls.append(self.domain + line)
            else:
                # 相对路径，拼接基础URL
                urls.append(urljoin(base_url, line))
        return urls

    def load(self):
        """加载并解析M3U8文件，返回片段URL列表"""
        os.makedirs(self.cache_dir, exist_ok=True)

        content = self._fetch_m3u8_content()
        lines = content.splitlines()

        # 1. 解析加密信息
        self._parse_encryption_info(lines)

        # 2. 解析TS片段URL
        base_url = self.url.rsplit("/", 1)[0] + "/" if self.url else (self.domain or "")
        urls = self._parse_segment_urls(lines, base_url)

        if urls:
            print(f"解析到 {len(urls)} 个视频片段")
        else:
            print("警告：未解析到任何TS片段")
        return urls

    async def _download_segment(self, session, sem, url, idx, total):
        """下载单个TS片段，带重试机制"""
        async with sem:
            for attempt in range(3):
                try:
                    async with session.get(url, ssl=ssl_context) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            # 异步写入缓存文件
                            async with aiofiles.open(f"{self.cache_dir}/{idx}.ts", "wb") as f:
                                await f.write(data)
                            print(f"✓ {idx}/{total}")
                            return True
                        else:
                            print(f"✗ {idx} (HTTP {resp.status})")
                except Exception as e:
                    print(f"✗ {idx} (尝试{attempt+1}/3): {e}")
                    await asyncio.sleep(1)
            return False

    async def _async_download(self, urls):
        """异步下载所有TS片段"""
        connector = aiohttp.TCPConnector(limit=self.max_concurrent, ssl=ssl_context)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=self.headers) as session:
            sem = asyncio.Semaphore(self.max_concurrent)
            tasks = [
                self._download_segment(session, sem, url, i, len(urls))
                for i, url in enumerate(urls)
            ]
            results = await asyncio.gather(*tasks)

        success_count = sum(results)
        print(f"下载完成：成功 {success_count}/{len(urls)}")
        return success_count

    def splice(self):
        """合并已下载的TS片段为完整视频文件"""
        if not os.path.exists(self.cache_dir):
            print("缓存目录不存在，无法合并")
            return

        # 获取所有.ts文件并按数字索引排序
        files = [f for f in os.listdir(self.cache_dir) if f.endswith(".ts")]
        if not files:
            print("没有找到任何TS片段，合并中止")
            return

        files.sort(key=lambda x: int(x.split(".")[0]))
        print(f"开始合并 {len(files)} 个片段...")

        try:
            with open(self.output_file, "wb") as out:
                for filename in files:
                    filepath = os.path.join(self.cache_dir, filename)
                    with open(filepath, "rb") as seg:
                        out.write(seg.read())
                    print(f"已合并: {filename}")
            print(f"合并完成，输出文件: {self.output_file}")
        except Exception as e:
            print(f"合并过程中出错: {e}")
            raise

    def _fetch_key(self):
        """下载或解析AES密钥字节"""
        if not self.key_uri:
            raise ValueError("密钥URI为空")
        print(f"正在获取密钥: {self.key_uri}")
        try:
            if self.key_uri.startswith("http"):
                resp = requests.get(self.key_uri, headers=self.headers, verify=False, timeout=10)
                resp.raise_for_status()
                key_bytes = resp.content
                print(f"密钥下载成功，长度: {len(key_bytes)}")
            else:
                # 若URI不是HTTP，则尝试直接作为十六进制字符串解析
                key_bytes = bytes.fromhex(self.key_uri) if len(self.key_uri) == 32 else self.key_uri.encode()
        except Exception as e:
            raise Exception(f"密钥获取失败: {e}")

        # AES密钥长度必须为16、24或32字节，若非则补齐/截断
        if len(key_bytes) not in (16, 24, 32):
            print(f"警告：密钥长度 {len(key_bytes)} 非标准，将调整为32字节")
            key_bytes = (key_bytes + b'\x00' * 32)[:32]
        return key_bytes

    def _prepare_iv(self):
        """将IV字符串转换为16字节二进制数据"""
        if len(self.iv_str) == 32:
            # 十六进制字符串（32字符表示16字节）
            iv_bytes = bytes.fromhex(self.iv_str)
        else:
            # 直接使用ASCII字符串，截断或补零到16字节
            iv_bytes = self.iv_str.encode()[:16]
            if len(iv_bytes) < 16:
                iv_bytes += b'\x00' * (16 - len(iv_bytes))
        return iv_bytes

    def decrypt(self):
        """AES-128 CBC解密合并后的视频文件"""
        if not self.is_encrypted:
            print("该视频未加密，无需解密")
            return False

        if not os.path.exists(self.output_file):
            print(f"加密文件 {self.output_file} 不存在，无法解密")
            return False

        try:
            # 1. 获取密钥和IV
            key_bytes = self._fetch_key()
            iv_bytes = self._prepare_iv()

            print(f"密钥长度: {len(key_bytes)}, IV: {iv_bytes.hex()}")
            # 2. 读取加密数据
            with open(self.output_file, "rb") as f:
                encrypted_data = f.read()

            # 3. 构造解密器并解密
            cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted_data = decryptor.update(encrypted_data) + decryptor.finalize()

            # 4. 移除PKCS7填充
            try:
                pad_len = decrypted_data[-1]
                if 1 <= pad_len <= 16:
                    decrypted_data = decrypted_data[:-pad_len]
            except IndexError:
                pass  # 数据为空

            # 5. 保存解密后的文件（加密视频解密后另存为新文件，原加密文件保留）
            decrypted_file = "decrypted_video.ts"
            with open(decrypted_file, "wb") as f:
                f.write(decrypted_data)

            print(f"解密成功！文件已保存为: {decrypted_file}")
            return True

        except Exception as e:
            print(f"解密失败: {e}")
            return False

    def _cleanup(self):
        """清理临时文件（缓存片段、M3U8文件）"""
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
            print(f"已删除缓存目录: {self.cache_dir}")
        if os.path.exists(self.m3u8_file):
            os.remove(self.m3u8_file)
            print(f"已删除临时M3U8文件: {self.m3u8_file}")

    def run(self):
        """执行完整下载流程：解析、下载、合并、解密"""
        try:
            # 1. 解析M3U8，获取片段URL列表
            urls = self.load()
            if not urls:
                print("没有需要下载的片段，退出")
                return

            # 2. 异步下载所有片段
            asyncio.run(self._async_download(urls))

            # 3. 合并片段为单个TS文件
            self.splice()

            # 4. 若加密则解密，否则直接输出未加密视频（已保存为 self.output_file）
            if self.is_encrypted:
                self.decrypt()
            else:
                # 未加密视频：合并后的 video.ts 即为最终文件，无需任何重命名
                print(f"未加密视频已保存为: {self.output_file}")

        except KeyboardInterrupt:
            print("\n用户主动中断下载")
        except Exception as e:
            print(f"运行过程中发生错误: {e}")
        finally:
            # 无论成功失败，都清理临时文件
            self._cleanup()


if __name__ == '__main__':
    url = input("请输入M3U8地址: ").strip()
    if url.startswith("http"):
        Downloader(url=url).run()
    else:
        print("URL输入有误！程序退出...ok!")
