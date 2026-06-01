# wechat-formatter

Ghost 博客 → 微信公众号 同步工具。支持双向管道：

```
markdown → Ghost 博客 → 微信公众号
```

## 功能

- **发布**：Markdown 文件直接发布到 Ghost 博客（Lexical 格式，编辑器可编辑）
- **同步**：已发布的 Ghost 文章同步到微信公众号草稿箱
- **全自动**：发布后一步同步到微信

## 安装

```bash
git clone https://github.com/yinguobing/wechat-formatter.git
cd wechat-formatter
pip install requests PyJWT
```

## 配置

创建 `config.json`：

```json
{
  "wechat": {
    "appid": "your_wechat_appid",
    "secret": "your_wechat_secret"
  },
  "ghost": {
    "api_url": "https://yinguobing.com",
    "admin_key_id": "your_ghost_admin_key_id",
    "admin_key": "your_ghost_admin_key_secret"
  }
}
```

**Ghost Admin API Key 获取：**
Ghost 后台 → Settings → Advanced → Integrations → Add custom integration → 复制 `Admin API Key`（格式为 `key_id:hex_secret`，在 config 中拆成两段填入）

**微信 AppID/Secret 获取：**
[微信公众平台](https://mp.weixin.qq.com) → 设置与开发 → 基本配置

## 用法

### 列出 Ghost 文章

```bash
python3 sync.py list
```

### 发布 Markdown 到 Ghost

```bash
# 直接发布
python3 sync.py publish article.md

# 指定标题和标签
python3 sync.py publish article.md --title "我的文章" --tags Ghost,开源

# 指定作者（slug，默认 xiaohei）
python3 sync.py publish article.md --author guobing

# 先存草稿
python3 sync.py publish article.md --draft

# 发布后自动同步到微信
python3 sync.py publish article.md --wechat
```

### 同步 Ghost 文章到微信草稿

```bash
# 先列出文章获取 ID
python3 sync.py list

# 同步指定文章
python3 sync.py <article-id>

# 预览 HTML（不创建草稿）
python3 sync.py --preview <article-id>
```

## 管道说明

### Markdown → Ghost（`publish` 命令）

将 Markdown 文件转换为 Ghost 的 **Lexical 格式**（基于 `@tryghost/kg-lexical-html-renderer`），支持：

- 标题（h1~h6）
- 段落、粗体、斜体、行内代码、链接
- 围栏代码块（带语言标记）
- 表格（以 HTML card 渲染）
- 有序/无序列表
- 分割线

转换后的文章在 Ghost 编辑器里可以正常编辑。

### Ghost → 微信（`sync` 命令）

从 Ghost API 获取文章 HTML，经过多层处理管道后推送到微信公众号草稿箱：

1. 图片上传到微信永久素材，替换为 CDN 地址
2. 白名单三层过滤（标签/属性/样式）
3. 代码块保护与恢复
4. 微信不支持的标签转换（table → div, ol/ul → 前缀段落, hr → 分隔线）
5. 默认样式补全

## 项目结构

```
wechat-formatter/
├── sync.py          # 主程序
└── config.json      # 配置文件（需自行创建）
```

## 注意事项

- 微信草稿标题限制：Unicode 特殊字符（弯引号、破折号等）会触发 45003 错误，脚本会自动处理
- 作者字段在微信公众号中限制 8 字节
- 代码块使用 `<pre>` + 语言标签的样式方案，微信中可用
- `--wechat` 参数需要在 Ghost API 中能通过 slug 查到刚发布的文章
