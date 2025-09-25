import re
import sys
import time
import os
import json
import base64
from typing import Iterable, List, Tuple
from lxml import etree
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.compat import urljoin

from chaojiying import Chaojiying_Client


URL_TEMPLATE = "https://price.21food.cn/guoshu-p{page}.html"
FIRST_PAGE = 1
LAST_PAGE = 29


def create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                "AppleWebKit/537.36 (KHTML, like Gecko)"
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "cookie": "JSESSIONID=aaaQR8ydPcmlxd3RfzVLz; Hm_lvt_130a926bf7ac0d05a0769ad61f7730b2=1758544294; HMACCOUNT=865E5BDB61CE30FE; __51vcke__3GzAxR3EGfmHrtLx=1130421f-22a6-543b-8044-19b679f0fe32; __51vuft__3GzAxR3EGfmHrtLx=1758544293565; clientkey=1758544300570_8247; searchkey=%u725B%u767E%u53F6; _ga=GA1.2.1832288571.1758544294; _gid=GA1.2.1319798846.1758800669; __51uvsct__3GzAxR3EGfmHrtLx=4; _clientkey_=58.246.206.98; _gat=1; visittimes=42; Hm_lpvt_130a926bf7ac0d05a0769ad61f7730b2=1758800735; __vtins__3GzAxR3EGfmHrtLx=%7B%22sid%22%3A%20%229e3ff344-70ea-5dbf-850c-514d28aec978%22%2C%20%22vd%22%3A%204%2C%20%22stt%22%3A%2066124%2C%20%22dr%22%3A%203387%2C%20%22expires%22%3A%201758802535379%2C%20%22ct%22%3A%201758800735379%7D; _ga_G8T1QC9BGW=GS2.2.s1758800669$o4$g1$t1758800735$j57$l0$h0; code=f; view=1391",
            "Referer": "https://price.21food.cn/guoshu-p1.html",
        }
    )
    return session


def fetch_html(session: requests.Session, page: int) -> str:
    url = URL_TEMPLATE.format(page=page)
    resp = session.get(url, timeout=10)
    # 站点通常为 utf-8；若服务器声明编码则信任，否则回退到 utf-8
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def _text(node: etree._Element) -> str:
    # 抽取节点所有可见文本并归一化空白
    if node is None:
        return ""
    raw = "".join(node.xpath(".//text()"))
    return re.sub(r"\s+", " ", raw).strip()


def extract_rows(html_text: str) -> List[Tuple[str, str, str, str]]:
    # 用 HTML 解析器构建 DOM
    doc = etree.HTML(html_text)
    if doc is None:
        return []

    results: List[Tuple[str, str, str, str]] = []

    # 优先选择包含目标表头关键字的 table
    candidate_tables = doc.xpath("//div[@class='sjs_top_cent_erv'][1]//li")

    for li in candidate_tables:
        tds = li.xpath(".//tr/td")
        product_name = tds[0].xpath(".//a/text()")[0].strip()
        spec = tds[1].xpath("string(.)").strip()
        price = tds[2].xpath("string(.)").strip()
        date = tds[3].xpath("string(.)").strip()
        results.append((product_name, spec, average_price, date_value))

    return results


def iter_pages(start: int, end: int) -> Iterable[int]:
    step = 1 if end >= start else -1
    for p in range(start, end + step, step):
        yield p


def maybe_handle_captcha(session: requests.Session, html_text: str, referer: str) -> None:
    """当页面出现人机校验时，尝试提取验证码图片，保存并调用打码平台识别。

    仅打印识别结果；不提交回站点。
    """
    if not html_text:
        return

    # 快速特征判断
    keywords = ("验证码", "人机校验", "人机访问检测", "请点击", "安全验证", "行为验证")
    if not any(k in html_text for k in keywords):
        # 进一步用结构判断：是否存在明显的验证码图片元素
        doc_probe = etree.HTML(html_text)
        if doc_probe is None:
            return
        img_nodes = doc_probe.xpath("//img[contains(@src, 'captcha') or contains(@src, 'verify') or contains(@src, 'checkcode')]")
        if not img_nodes:
            return
        doc = doc_probe
    else:
        doc = etree.HTML(html_text)
        if doc is None:
            return

    # 查找验证码图片
    img_node = None
    candidates = doc.xpath('//img[@class="back-img"]')
    if candidates:
        img_node = candidates[0]

    if img_node is None:
        # 尝试从 style 背景图提取
        bg_el = doc.xpath("//*[@style and contains(translate(@style,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'background')][1]")
        if bg_el:
            style = bg_el[0].get("style", "")
            m = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
            if m:
                img_src = m.group(1)
                _download_and_solve_captcha(session, img_src, referer)
                return
        return

    img_src = img_node.get("src", "").strip()
    if not img_src:
        return

    _download_and_solve_captcha(session, img_src, referer)


def _download_and_solve_captcha(session: requests.Session, img_src: str, referer: str) -> None:
    # 解析并下载图片字节
    if img_src.startswith("data:image"):
        # base64 数据
        base64_part = img_src.split(",", 1)[1] if "," in img_src else ""
        if not base64_part:
            return
        img_bytes = base64.b64decode(base64_part)
    else:
        img_url = urljoin(referer, img_src)
        resp = session.get(img_url, headers={"Referer": referer}, timeout=10)
        resp.raise_for_status()
        img_bytes = resp.content

    # 保存到项目路径
    project_dir = os.path.dirname(os.path.abspath(__file__))
    ts = int(time.time())
    img_path = os.path.join(project_dir, f"captcha_{ts}.jpg")
    with open(img_path, "wb") as f:
        f.write(img_bytes)

    # 读取平台配置
    config_path = os.path.join(project_dir, "config.json")
    if not os.path.exists(config_path):
        print("[INFO] 未找到 config.json，跳过打码上传", file=sys.stderr)
        return
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    username = cfg.get("username", "")
    password = cfg.get("password", "")
    soft_id = cfg.get("soft_id", "")

    if not (username and password and soft_id):
        print("[INFO] config.json 配置不完整，跳过打码上传", file=sys.stderr)
        return

    client = Chaojiying_Client(username, password, soft_id)
    # 验证码类型：此处采用 4004 作为通用点击/坐标类示例；如不匹配可调整
    result = client.PostPic(img_bytes, 4004)
    # 打印返回结果（包含识别文本/坐标等字段）
    try:
        print(json.dumps({"captcha_image": os.path.basename(img_path), "result": result}, ensure_ascii=False))
    except Exception:
        print({"captcha_image": os.path.basename(img_path), "result": result})



def main() -> None:
    session = create_session()
    print("产品名称\t规格\t平均价格\t日期")
    for page in iter_pages(FIRST_PAGE, LAST_PAGE):
        try:
            html_text = fetch_html(session, page)
        except Exception as exc:
            print(f"[WARN] 第{page}页请求失败: {exc}", file=sys.stderr)
            continue

        # 处理可能的人机校验/验证码
        try:
            maybe_handle_captcha(session, html_text, referer=URL_TEMPLATE.format(page=page))
        except Exception as exc:
            print(f"[WARN] 第{page}页验证码处理失败: {exc}", file=sys.stderr)

        rows = extract_rows(html_text)
        if not rows:
            # 有些页面首屏为“页面加载中”，短暂等待后重试一次
            if "页面加载中" in html_text:
                time.sleep(0.8)
                try:
                    html_text = fetch_html(session, page)
                    # 重试前再次尝试处理验证码
                    try:
                        maybe_handle_captcha(session, html_text, referer=URL_TEMPLATE.format(page=page))
                    except Exception:
                        pass
                    rows = extract_rows(html_text)
                except Exception as exc:
                    print(f"[WARN] 第{page}页重试失败: {exc}", file=sys.stderr)

        if not rows:
            print(f"[WARN] 第{page}页未解析到数据", file=sys.stderr)
            continue

        for product_name, specification, average_price, date_value in rows:
            print(f"{product_name}\t{specification}\t{average_price}\t{date_value}")

if __name__ == "__main__":
    main()


