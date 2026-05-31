#!/usr/bin/env python3
"""
Ghost → 微信公众号 同步脚本

用法:
  python3 sync.py list                     列出 Ghost 文章
  python3 sync.py <article-id>             同步指定文章到微信草稿箱
  python3 sync.py --preview <article-id>   预览生成的 HTML（不创建草稿）

前提:
  - config.json 已配置（公众号 appid/secret + Ghost admin key）
  - 运行在阿里云服务器（IP: 8.134.118.96），已在微信 IP 白名单

同步内容:
  - 标题（Unicode 引号/破折号自动转为 ASCII，避免微信 45003 错误）
  - 作者：「国冰」（6字节，超出则回退空字符串）
  - 副标题：使用 Ghost 的 custom_excerpt，超长自动截断
  - 封面图 + 内容图片：上传到微信永久素材，替换为 CDN 地址
  - 段落间距：margin-bottom: 16px
  - 图片间距：margin-bottom: 16px
  - 有序列表：转为 <p> + 数字前缀（微信不渲染 <ol> 样式）
  - 无序列表：转为 <p> + 圆点前缀
  - 链接：转为 text [url] 格式
  - 代码块：保留样式和语言标记
"""

import json, sys, re, os, requests, jwt, time, textwrap, html.parser
from html import escape as _html_escape
from urllib.parse import urlparse

# 代码块占位符，避免被 HTML 清理破坏
# ── 微信安全标签白名单 ──────────────────────────────────────
# 用于 clean_html_for_wechat 的三层过滤
WECHAT_SAFE_TAGS = {
    "p", "br", "strong", "em", "b", "i", "u", "a", "img", "span",
    "div", "h2", "h3", "h4", "blockquote", "pre", "code", "ul", "ol", "li",
}
WECHAT_SAFE_ATTRS = {"href", "src", "alt", "title"}
WECHAT_SAFE_STYLES = {
    "color", "font-size", "font-weight", "font-family", "text-align",
    "line-height", "margin", "margin-bottom", "margin-left", "margin-right",
    "padding", "padding-left", "padding-right", "background", "background-color",
    "border", "border-left", "border-radius", "width", "height", "max-width",
    "white-space", "word-break", "overflow",
}

_CODE_PLACEHOLDER = "__CODE_BLOCK_PLACEHOLDER__"
_code_blocks_cache = []

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
TOKEN_CACHE = "/tmp/wechat_token.json"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[!] 配置不存在: {CONFIG_PATH}")
        print("[!] 请创建 config.json，参考:")
        print(textwrap.dedent("""\
        {
          "wechat": {
            "appid": "your_appid",
            "secret": "your_secret"
          },
          "ghost": {
            "api_url": "https://yinguobing.com",
            "admin_key_id": "xxx",
            "admin_key": "hex_secret"
          }
        }"""))
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ── 微信 Token ────────────────────────────────────────────────
def get_wechat_token(appid, secret):
    cached = None
    if os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE) as f:
            cached = json.load(f)
        if cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

    url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise Exception(f"获取token失败: {data}")

    token = data["access_token"]
    with open(TOKEN_CACHE, "w") as f:
        json.dump({
            "access_token": token,
            "expires_at": data.get("expires_in", 7200) + time.time()
        }, f)
    return token


# ── Ghost Token ────────────────────────────────────────────────
def get_ghost_token(key_id, key_secret):
    # secret 需要从 hex 解码为原始字节
    secret_bytes = bytes.fromhex(key_secret)
    now = int(time.time())
    payload = {"aud": "/admin/", "iat": now, "exp": now + 300, "type": "admin"}
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    return jwt.encode(payload, secret_bytes, algorithm="HS256", headers=header)


# ── Ghost API ─────────────────────────────────────────────────
def ghost_api_get(path, config):
    key_id = config["ghost"]["admin_key_id"]
    key_secret = config["ghost"]["admin_key"]
    api_url = config["ghost"]["api_url"]
    token = get_ghost_token(key_id, key_secret)
    headers = {"Authorization": f"Ghost {token}", "Content-Type": "application/json"}
    r = requests.get(f"{api_url}{path}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def get_ghost_posts(config, limit=20, status="all"):
    return ghost_api_get(f"posts/?limit={limit}&status={status}", config)


def get_ghost_article(article_id, config):
    return ghost_api_get(f"posts/{article_id}/?formats=html", config)


# ── 微信素材上传 ──────────────────────────────────────────────
def upload_permanent_material(token, image_url, material_type="image"):
    """上传永久素材，返回 (media_id, url)"""
    try:
        img_data = requests.get(image_url, timeout=30)
        img_data.raise_for_status()
    except Exception as e:
        print(f"  [警告] 下载图片失败 {image_url}: {e}")
        return None, None

    ext = re.search(r'\.(jpg|jpeg|png|gif|webp)', image_url, re.I)
    ext = ext.group(1) if ext else "jpg"
    mime = f"image/{ext.replace('jpg', 'jpeg')}"

    files = {"media": (f"image.{ext}", img_data.content, mime)}
    url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={token}&type={material_type}"
    r = requests.post(url, files=files, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "media_id" in data:
        return data["media_id"], data.get("url")
    print(f"  [警告] 上传永久素材失败: {data}")
    return None, None


# ── 代码块转换 ──────────────────────────────────────────────
def convert_code_blocks(html):
    """将 <pre><code> 转为占位符，保护内容不被后续清理破坏

    分两步：
    1. 提取代码块为占位符（在 clean 之前），保存内容和语言标记
    2. clean 之后调 restore_code_blocks() 恢复为带样式的 HTML
    """
    global _code_blocks_cache
    _code_blocks_cache = []

    def _extract(match):
        full = match.group(0)
        # 提取语言
        lang_match = re.search(r'class="language-(\w+)"', full)
        lang = lang_match.group(1) if lang_match else ""
        # 提取代码内容（<code> 标签内部）
        code_match = re.search(r'<code[^>]*>(.*?)</code>', full, re.DOTALL)
        code_content = code_match.group(1) if code_match else match.group(1)
        idx = len(_code_blocks_cache)
        _code_blocks_cache.append({"lang": lang, "content": code_content})
        return f"{_CODE_PLACEHOLDER}{idx}{_CODE_PLACEHOLDER}"

    # 先匹配 <pre><code ...>...</code></pre>（最常用）
    html = re.sub(r'<pre><code[^>]*>(.*?)</code></pre>', _extract, html, flags=re.DOTALL)
    # 再匹配纯 <pre>...</pre>（无 <code> 包裹）
    html = re.sub(r'<pre>(.*?)</pre>', _extract, html, flags=re.DOTALL)
    return html


def restore_code_blocks(html):
    """将占位符恢复为带样式的代码块 HTML"""
    global _code_blocks_cache

    def _restore(match):
        idx = int(match.group(1))
        block = _code_blocks_cache[idx]
        content = block["content"]
        lang = block["lang"]

        # 语言标签放在 <code> 外面、<pre> 内部
        lang_html = ""
        if lang:
            lang_html = (
                f'<div style="font-size: 12px; color: #999; '
                f'margin-bottom: 6px; line-height: 1.4;">'
                f'{_html_escape(lang)}'
                f'</div>'
            )

        return (
            '<pre style="background: #f5f5f5; padding: 12px 16px; '
            'border-radius: 4px; font-size: 14px; line-height: 1.7; '
            'overflow-x: auto; margin-bottom: 16px; white-space: pre-wrap; '
            'word-break: break-all; border: 1px solid #e0e0e0;">'
            f'{lang_html}'
            '<code style="font-family: Consolas, Monaco, \'Courier New\', '
            'monospace; white-space: pre-wrap; color: #333;">'
            f'{content}'
            '</code></pre>'
        )

    return re.sub(
        rf'{_CODE_PLACEHOLDER}(\d+){_CODE_PLACEHOLDER}',
        _restore,
        html
    )


# ── 白名单辅助函数 ──────────────────────────────────────────
def _filter_style(style_value):
    """仅保留白名单内的 CSS 属性"""
    props = []
    for decl in style_value.split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop = prop.strip().lower()
        if prop in WECHAT_SAFE_STYLES:
            props.append(f"{prop}: {val.strip()}")
    return "; ".join(props)


class _WeChatCleaner(html.parser.HTMLParser):
    """基于白名单的三层 HTML 过滤器

    Level 1: 标签白名单 — 不在白名单的标签去掉，保留内容
    Level 2: 属性白名单 — 只保留安全属性
    Level 3: 样式白名单 — 只保留安全 CSS 属性
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth > 0:
            self._skip_depth += 1
            return
        if tag not in WECHAT_SAFE_TAGS:
            self._skip_depth = 1
            return
        keep = []
        for name, val in attrs:
            nl = name.lower().strip()
            if nl in WECHAT_SAFE_ATTRS:
                keep.append(f'{name}="{_html_escape(val)}"')
            elif nl == "style" and val.strip():
                filtered = _filter_style(val)
                if filtered:
                    keep.append(f'style="{_html_escape(filtered)}"')
        attr_str = " " + " ".join(keep) if keep else ""
        self.out.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in WECHAT_SAFE_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.out.append(data)

    def handle_entityref(self, name):
        if self._skip_depth == 0:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if self._skip_depth == 0:
            self.out.append(f"&#{name};")

    def handle_comment(self, data):
        pass  # comments removed entirely


# ── 清理微信不支持的 HTML ─────────────────────────────────────
def clean_html_for_wechat(html):
    """三层白名单过滤

    1. 移除 <script>/<style> 及其内容
    2. 标签白名单 — 不在 WECHAT_SAFE_TAGS 的标签去掉，保留内容
    3. 属性白名单 — 只保留 WECHAT_SAFE_ATTRS 中的属性
    4. 样式白名单 — 只保留 WECHAT_SAFE_STYLES 中的 CSS 属性
    5. 替换 data-src 为 src
    """
    # 移除 script/style
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.I)
    # 替换 data-src 为 src（在 parser 处理之前做，避免 attr 被过滤）
    html = re.sub(r'\s*data-src="([^"]+)"', r' src="\1"', html)
    # 三层过滤
    cleaner = _WeChatCleaner()
    cleaner.feed(html)
    return "".join(cleaner.out)


# ── Ghost 注释清理 ──────────────────────────────────────────
def clean_ghost_comments(html):
    """移除 Ghost 特有 HTML 注释（<!--kg-card-*--> 等）"""
    return re.sub(r'<!--[\s\S]*?-->', '', html)


# ── 水平分隔线 ────────────────────────────────────────────────
def convert_hr(html):
    """将 <hr> 转为带样式的分隔线"""
    return re.sub(
        r'<hr[^>]*>',
        r'<div style="border-top: 1px solid #ddd; margin: 24px 0;"></div>',
        html
    )


# ── 嵌套 blockquote 清理 ─────────────────────────────────────
def flatten_nested_blockquotes(html):
    """展平嵌套 blockquote（微信不支持嵌套渲染）"""
    while "<blockquote><blockquote>" in html:
        html = html.replace("<blockquote><blockquote>", "<blockquote>")
    while "</blockquote></blockquote>" in html:
        html = html.replace("</blockquote></blockquote>", "</blockquote>")
    return html


# ── 默认样式补全 ──────────────────────────────────────────────
def apply_wechat_styles(html):
    """在清理后补全微信兼容的默认样式

    修复项（按优先级）：
    1. heading 字号（h2/h3/h4）
    2. 内联 code 背景色
    5. blockquote 引用样式
    6. inline code 字体样式
    """
    # Fix 1: heading 字号
    html = re.sub(
        r'<h2(\b(?!\s+[^>]*style=)[^>]*)>',
        r'<h2 style="font-size: 20px; font-weight: bold; margin-bottom: 12px; margin-top: 24px;"\1>',
        html
    )
    html = re.sub(
        r'<h3(\b(?!\s+[^>]*style=)[^>]*)>',
        r'<h3 style="font-size: 18px; font-weight: bold; margin-bottom: 10px; margin-top: 20px;"\1>',
        html
    )
    html = re.sub(
        r'<h4(\b(?!\s+[^>]*style=)[^>]*)>',
        r'<h4 style="font-size: 16px; font-weight: bold; margin-bottom: 8px; margin-top: 16px;"\1>',
        html
    )

    # Fix 2 & 6: 内联 code 背景色 + 字体样式
    # 只匹配没有 style 属性的 <code>（<pre> 内的代码块已有样式）
    html = re.sub(
        r'<code(\b(?!\s+[^>]*style=)[^>]*)>',
        r'<code style="background: #f0f0f0; padding: 2px 4px; border-radius: 3px; '
        r'font-size: 14px; font-family: Consolas, Monaco, \'Courier New\', monospace; '
        r'color: #333;"\1>',
        html
    )

    # Fix 5: blockquote 引用样式
    html = re.sub(
        r'<blockquote(\b(?!\s+[^>]*style=)[^>]*)>',
        r'<blockquote style="border-left: 4px solid #ddd; '
        r'padding: 8px 16px; margin: 16px 0; '
        r'color: #666; background: #fafafa;"\1>',
        html
    )

    return html


def extract_images(html):
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


def replace_images(html, image_map):
    """把 HTML 中的原始图片 URL 替换为微信 CDN URL"""
    for old_url, wechat_url in image_map.items():
        html = html.replace(f'src="{old_url}"', f'src="{wechat_url}"')
    return html


# ── HTML 元素转换（微信不原生支持的标签）─────────────────────
def convert_links(html):
    """将 <a href="url">text</a> 转为 text [url]

    微信草稿箱的 <a> 标签可能会被过滤或样式丢失，
    转为纯文本 + URL 更可靠。
    """
    def _replace_link(match):
        href = match.group(1)
        text = match.group(2)
        text = text.strip()
        if href == text or href.endswith(text):
            # 链接文本就是 URL 本身，不用重复
            return text
        return f'{text} [{href}]'

    html = re.sub(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        _replace_link,
        html,
        flags=re.DOTALL
    )
    # 也处理单引号
    html = re.sub(
        r"<a\s+[^>]*href='([^']+)'[^>]*>(.*?)</a>",
        _replace_link,
        html,
        flags=re.DOTALL
    )
    return html


def convert_ordered_list(html):
    """将 <ol> 转为 <p> + 数字前缀

    微信不渲染 <ol> 列表样式，转为带编号的段落。
    """
    def _convert(match):
        items = re.findall(r'<li>(.*?)</li>', match.group(0), re.DOTALL)
        lines = []
        for i, item in enumerate(items, 1):
            item = item.strip()
            lines.append(f'<p style="margin-bottom: 8px; padding-left: 16px;">{i}. {item}</p>')
        return '\n'.join(lines)

    return re.sub(r'<ol>.*?</ol>', _convert, html, flags=re.DOTALL)


def convert_unordered_list(html):
    """将 <ul> 转为 <p> + 圆点前缀

    微信不渲染 <ul> 列表样式，转为带 bullet 的段落。
    """
    def _convert(match):
        items = re.findall(r'<li>(.*?)</li>', match.group(0), re.DOTALL)
        lines = []
        for item in items:
            item = item.strip()
            lines.append(f'<p style="margin-bottom: 8px; padding-left: 16px;">• {item}</p>')
        return '\n'.join(lines)

    return re.sub(r'<ul>.*?</ul>', _convert, html, flags=re.DOTALL)


# ── 创建微信草稿 ──────────────────────────────────────────────
def create_wechat_draft(token, title, author, content, thumb_media_id, digest=""):
    # digest 默认用 custom_excerpt，超长则截断
    if not digest:
        digest = "查看全文"
    elif len(digest.encode("utf-8")) > 120:
        digest = digest.encode("utf-8")[:119].decode("utf-8", errors="ignore")
    articles = [{
        "title": title,
        "author": author,
        "content": content,
        "content_source_url": "",
        "digest": digest,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    }]
    url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={token}"
    # 手动序列化确保中文字符不被转义为 \uXXXX
    payload_bytes = json.dumps({"articles": articles}, ensure_ascii=False).encode("utf-8")
    r = requests.post(url, data=payload_bytes, headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("errcode") == 0 or "media_id" in data:
        return True, f"草稿创建成功，media_id={data.get('media_id')}"
    return False, f"创建草稿失败: {data}"


# ── HTML 处理管道 ────────────────────────────────────────────
def process_html(html_content, image_map):
    """完整的 HTML 处理管道

    流程（按顺序）：
    1. 替换图片 URL
    2. 移除 Ghost 注释（<!--kg-card-*-->）
    3. 转换 <hr> 为带样式分隔线
    4. 保护代码块（提取为占位符）
    5. 三层白名单过滤（标签/属性/样式）
    6. 恢复代码块
    7. 补全默认样式（heading 字号、内联 code、blockquote）
    8. 展平嵌套 blockquote
    9. 段落间距
    10. 图片间距
    11. 链接转文本+URL
    12. 有序列表转为数字前缀段落
    13. 无序列表转为圆点前缀段落

    返回处理后可在微信草稿中使用的 HTML。
    """
    # 1. 替换图片 URL
    html = replace_images(html_content, image_map)

    # 2. 移除 Ghost 特有注释
    html = clean_ghost_comments(html)

    # 3. 转换 <hr> 为带样式分隔线（必须在清理之前，因为 <hr> 不在白名单）
    html = convert_hr(html)

    # 4. 保护代码块（提取为占位符，避免被后续清理破坏）
    html = convert_code_blocks(html)

    # 5. 三层白名单过滤
    html = clean_html_for_wechat(html)

    # 6. 恢复代码块（带样式）
    html = restore_code_blocks(html)

    # 7. 补全默认样式
    html = apply_wechat_styles(html)

    # 8. 展平嵌套 blockquote（微信不支持嵌套渲染）
    html = flatten_nested_blockquotes(html)

    # 9. 段落间距
    html = re.sub(r'<p\b(?!\s+[^>]*style=)([^>]*)>', r'<p style="margin-bottom: 16px;">', html)

    # 10. 图片间距
    html = re.sub(r'<img([^>]*)>', r'<img style="margin-bottom: 16px;"\1>', html)

    # 11. 链接转换（微信对 <a> 支持不稳定）
    html = convert_links(html)

    # 12. 列表转换（微信不渲染 <ol>/<ul> 的列表样式）
    html = convert_ordered_list(html)
    html = convert_unordered_list(html)

    return html


# ── 主同步逻辑 ────────────────────────────────────────────────
def sync_article(article_id, preview_only=False):
    config = load_config()
    wc = config["wechat"]

    action = "预览" if preview_only else "同步"
    print(f"[*] 开始{action} Ghost 文章 {article_id} → 微信草稿")

    # 1. 获取微信 token（预览模式也保留，因为后面上传图片需要）
    print("[*] 获取微信 access_token...")
    token = get_wechat_token(wc["appid"], wc["secret"])
    print(f"[+] token: {token[:20]}...")

    # 2. 获取 Ghost 文章
    print("[*] 获取 Ghost 文章...")
    ghost_data = get_ghost_article(article_id, config)
    posts = ghost_data.get("posts", [])
    if not posts:
        print(f"[!] 未找到文章: {article_id}")
        return False
    article = posts[0]

    title = article.get("title", "无标题")
    # 规范化标题：将 Unicode 特殊字符替换为 ASCII，避免微信 45003 错误
    title = (
        title
        .replace('\u201c', '"').replace('\u201d', '"')   # "" -> ""
        .replace('\u2018', "'").replace('\u2019', "'")   # '' -> ''
        .replace('\u2014', '-').replace('\u2013', '-')   # —– -> -
        .replace('\u3000', ' ')                           # 全角空格 -> 空格
    )
    author = article.get("primary_author", {}).get("name", "")
    html_content = article.get("html", "")
    feature_image = article.get("feature_image")
    custom_excerpt = article.get("custom_excerpt", "")

    print(f"[+] 标题: {title}")
    print(f"[+] 状态: {article.get('status')}")
    # 作者字段：微信限制最多 8 字节，超出则用 "国冰"（6字节）
    author_for_wechat = "国冰"
    if author and len(author.encode("utf-8")) <= 8:
        author_for_wechat = author

    # 3. 上传封面图（永久素材）
    thumb_media_id = ""
    if feature_image:
        print(f"[*] 上传封面图（永久素材）...")
        thumb_media_id, _ = upload_permanent_material(token, feature_image)
        if thumb_media_id:
            print(f"[+] 封面图 media_id: {thumb_media_id}")
        else:
            print(f"[!] 封面上传失败，将创建无封面草稿")

    # 4. 提取并上传内容图片
    images = extract_images(html_content)
    image_map = {}
    if images:
        print(f"[*] 发现 {len(images)} 张内容图片，开始上传...")
        for img_url in images:
            media_id, wechat_url = upload_permanent_material(token, img_url)
            if wechat_url:
                image_map[img_url] = wechat_url
                print(f"  [+] {img_url[:60]}... → {wechat_url[:60]}...")

    # 5. 通过 HTML 处理管道
    final_html = process_html(html_content, image_map)

    # 6. 输出结果
    print(f"[*] 标题字节: {len(title.encode('utf-8'))} | 作者: {author_for_wechat!r}")
    print(f"[*] 最终 HTML 预览:\n{final_html[:500]}...")
    print(f"[*] HTML 总长: {len(final_html)} 字符")

    if preview_only:
        # 预览模式：输出完整 HTML 到文件，方便检查
        preview_path = f"/tmp/wechat_preview_{article_id}.html"
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        print(f"\n[+] 完整 HTML 已保存到: {preview_path}")
        print(f"[+] 用浏览器打开即可预览效果")
        return True

    print("[*] 创建微信草稿...")
    success, msg = create_wechat_draft(token, title, author_for_wechat, final_html, thumb_media_id, custom_excerpt)
    print(f"[+] {msg}")
    return success


def list_posts():
    config = load_config()
    data = get_ghost_posts(config, limit=20, status="all")
    posts = data.get("posts", [])
    print(f"共 {len(posts)} 篇:\n")
    for p in posts:
        tag = "📝" if p.get("status") == "published" else "📄"
        print(f"{tag} [{p.get('status')}] {p.get('title')} | id={p.get('id')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 sync.py list              - 列出文章")
        print("  python3 sync.py <article-id>      - 同步到微信草稿")
        print("  python3 sync.py --preview <id>    - 预览 HTML（不创建草稿）")
        sys.exit(1)

    if sys.argv[1] == "list":
        list_posts()
    elif sys.argv[1] == "--preview" and len(sys.argv) > 2:
        article_id = sys.argv[2]
        try:
            sync_article(article_id, preview_only=True)
        except Exception as e:
            print(f"[!] 错误: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        article_id = sys.argv[1]
        try:
            sync_article(article_id)
        except Exception as e:
            print(f"[!] 错误: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
