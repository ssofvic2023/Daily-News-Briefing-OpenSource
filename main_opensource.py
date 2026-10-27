import os
import feedparser
import smtplib
import json
import re
import time
import google.generativeai as genai
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

# ================= 🔴 配置区域 (Community Edition) =================

# 1. API & 邮件凭证 (优先从环境变量读取)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "")

# 2. 隐私收件人 (可选：从 Secrets 读取，防止在 config.json 中暴露)
ENV_RECEIVER = os.environ.get("RECEIVER_EMAIL", "") 

# 3. SMTP 服务器配置
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465  # SSL 端口

# 4. 环境判断
if not os.environ.get("GITHUB_ACTIONS"):
    print("🏠 本地运行模式")
else:
    print("☁️ 云端运行模式")

# ================= 🛠️ 初始化逻辑 =================

# 初始化 Gemini
if not GEMINI_API_KEY:
    print("❌ 错误: 未找到 GEMINI_API_KEY 环境变量")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')

def load_config():
    """从本地 JSON 文件读取配置"""
    config_path = 'config.json'
    if not os.path.exists(config_path):
        print(f"❌ 错误: 找不到 {config_path} 配置文件")
        return None
        
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 读取 config.json 失败: {e}")
        return None

# ================= 🧠 核心业务逻辑 =================

class NewsDatabase:
    """内存新闻库 (仅本次运行有效)"""
    def __init__(self):
        self.items = {} 
        self.current_id = 1

    def add(self, source_name, entry):
        title = entry.title.strip()
        link = entry.link
        summary = getattr(entry, 'summary', '')[:250]
        
        self.items[self.current_id] = {
            "id": self.current_id,
            "source": source_name,
            "title": title,
            "link": link,
            "summary": summary
        }
        self.current_id += 1
        return self.current_id - 1

    def generate_prompt_text(self):
        text_block = ""
        for idx, item in self.items.items():
            text_block += f"[ID: {idx}] Title: {item['title']} | Source: {item['source']} | Context: {item['summary']}\n"
        return text_block

    def get_link_by_id(self, idx):
        if idx in self.items: return self.items[idx]['link']
        return "#"

db = NewsDatabase()

def fetch_all_rss(sources_dict):
    """抓取模块"""
    print("📡 正在扫描 RSS 源...")
    total_fetched = 0
    MAX_ITEMS_PER_SOURCE = 10 
    
    for category, url in sources_dict.items():
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                print(f"   ⚠️ [{category}] 无内容或连接失败")
                continue
                
            entries = feed.entries[:MAX_ITEMS_PER_SOURCE]
            print(f"   -> [{category}] +{len(entries)}")
            for entry in entries:
                db.add(category, entry)
                total_fetched += 1
        except Exception as e:
            print(f"   ❌ {category} 失败: {e}")
            
    print(f"📦 共入库 {total_fetched} 条新闻。")
    return total_fetched

def analyze_market_trends():
    """AI 分析模块"""
    news_text_block = db.generate_prompt_text()
    if not news_text_block: return None

    print("\n🧠 正在进行 AI 分析 (宏观 + 情绪)...")

    prompt = f"""
    You are a Quantitative Financial Analyst.
    
    # RAW NEWS:
    \"\"\"
    {news_text_block}
    \"\"\"

    # TASKS:
    **Task 1: Market Sentiment Scoring**
    - Score from -10 (Extreme Fear) to +10 (Extreme Greed).
    - Provide a one-sentence explanation in Chinese.
    
    **Task 2: Macro Analysis**
    - Write a 300-word summary in Chinese.
    - STRICT CITATION FORMAT: You MUST use `[1]`, `[2]` format. Do NOT use `[ID:1]`.
    
    **Task 3: Top 5 Picks**
    - Select 5 critical stories with `id`, `reason` (Chinese), and `tag`.

    # OUTPUT JSON:
    {{
        "sentiment_score": 5.5,
        "sentiment_label": "Modestly Bullish",
        "sentiment_reason": "...",
        "analysis_html": "...",
        "top_picks": [ {{ "id": 1, "reason": "...", "tag": "Bullish" }} ]
    }}
    """
    
    try:
        response = model.generate_content(prompt)
        cleaned_text = re.sub(r"```json|```", "", response.text).strip()
        return json.loads(cleaned_text)
    except Exception as e:
        print(f"❌ AI 分析失败: {e}")
        return None

def process_citations(text):
    """引用链接处理 (正则增强版)"""
    def replace_match(match):
        idx = int(match.group(1))
        link = db.get_link_by_id(idx)
        return f' <a href="{link}" style="color:#0056b3; text-decoration:none; font-weight:bold;">[{idx}]</a>'
    
    return re.sub(r'\[(?:ID\s*:?\s*)?(\d+)\]', replace_match, text, flags=re.IGNORECASE)

def get_sentiment_color(score):
    try:
        s = float(score)
        if s >= 6: return "#28a745"
        if s >= 2: return "#5cdb5c"
        if s <= -6: return "#dc3545"
        if s <= -2: return "#ff6b6b"
        return "#6c757d"
    except: return "#6c757d"

def generate_email_html(ai_result):
    score = ai_result.get('sentiment_score', 0)
    label = ai_result.get('sentiment_label', 'Neutral')
    reason = ai_result.get('sentiment_reason', 'No data')
    color = get_sentiment_color(score)
    
    raw_analysis = ai_result.get('analysis_html', '').replace("\n", "<br>")
    final_analysis = process_citations(raw_analysis)
    
    picks_html = ""
    for pick in ai_result.get('top_picks', []):
        pid = pick['id']
        tag = pick.get('tag', 'Neutral')
        tag_color = "#28a745" if "Bull" in tag else ("#dc3545" if "Bear" in tag else "#6c757d")
        
        if pid in db.items:
            item = db.items[pid]
            picks_html += f"""
            <div class="pick-card" style="background:#fff; padding:15px; margin-bottom:12px; border-radius:8px; border:1px solid #eee;">
                <div class="pick-header">
                    <span style="background:{tag_color}; color:white; padding:2px 6px; border-radius:3px; font-size:10px;">{tag}</span>
                    <a href="{item['link']}" style="text-decoration:none; color:#000; font-weight:bold;">{item['title']}</a>
                </div>
                <div style="margin-top:8px; font-size:13px; color:#666;">
                    <span style="background:#eee; padding:2px 5px;">{item['source']}</span> 💡 {pick['reason']}
                </div>
            </div>
            """

    today = (datetime.now() + timedelta(hours=8)).strftime("%Y-%m-%d")

    html = f"""
    <html>
    <head><style>body{{font-family:'Segoe UI',sans-serif;max-width:700px;margin:0 auto;padding:20px;background:#f4f6f9;color:#333;}}</style></head>
    <body>
        <div style="background:#fff; padding:20px; border-radius:12px; text-align:center; border-top:5px solid {color}; margin-bottom:25px;">
            <div style="font-size:12px; color:#999;">MARKET SENTIMENT INDEX</div>
            <div style="font-size:48px; font-weight:bold; color:{color};">{score}</div>
            <div style="font-size:18px; font-weight:600;">{label}</div>
            <div style="font-style:italic; color:#777; margin-top:10px;">"{reason}"</div>
        </div>
        <h3>📊 全球市场宏观综述</h3>
        <div style="background:#fff; padding:25px; border-radius:8px; line-height:1.8;">{final_analysis}</div>
        <h3>🔥 核心关注</h3>
        {picks_html}
        <div style="text-align:center; font-size:12px; color:#aaa; margin-top:40px;">{today} • Community Edition</div>
    </body></html>
    """
    return html

def send_email_to_list(html_body, receivers):
    """发送邮件给列表中的所有用户"""
    if not receivers: 
        print("📭 收件人列表为空，跳过发送。")
        return

    beijing_time = datetime.now() + timedelta(hours=8)
    date_str = beijing_time.strftime('%m-%d')
    subject = f"【早报】全球市场洞察 & 每日精选 ({date_str})"

    try:
        print("🔌 正在连接 SMTP 服务器...")
        print(f"📧 SENDER_EMAIL: [{SENDER_EMAIL}]")  # ← 添加这行
        print(f"🔑 密码长度: {len(SENDER_PASSWORD)}")  # ← 添加这行
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        
        for r in receivers:
            print(f"   -> 发送给: {r} ...")
            msg = MIMEText(html_body, 'html', 'utf-8')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = SENDER_EMAIL
            msg['To'] = r
            server.sendmail(SENDER_EMAIL, r, msg.as_string())
            time.sleep(2)
            
        server.quit()
        print("✅ 全部发送完成。")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

# ================= 🚀 主程序入口 =================

if __name__ == "__main__":
    # 1. 读取本地配置 (JSON)
    config = load_config()
    if not config: exit(1)

    rss_sources = config.get('rss_sources', {})
    
    # 2. 智能合并收件人 (JSON + Environment)
    receivers = config.get('receivers', [])
    
    # 如果环境变量里配置了 RECEIVER_EMAIL (适合 GitHub Secrets 场景)
    if ENV_RECEIVER:
        secret_receivers = [r.strip() for r in ENV_RECEIVER.split(',') if r.strip()]
        receivers.extend(secret_receivers)
        print(f"🔒 已加载 {len(secret_receivers)} 个隐私收件人")
    
    # 去重
    receivers = list(set(receivers))

    if not rss_sources:
        print("❌ 配置错误: rss_sources 为空")
        exit(1)
    
    # 3. 执行抓取
    if fetch_all_rss(rss_sources) > 0:
        # 4. 执行分析
        res = analyze_market_trends()
        if res:
            # 5. 执行发送
            if receivers:
                html = generate_email_html(res)
                send_email_to_list(html, receivers)
            else:
                print("📭 收件人列表为空 (仅运行分析，不发送)")


