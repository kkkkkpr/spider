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
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Referer": "https://price.21food.cn/",
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
    candidate_tables = doc.xpath(
        """
        //table[
          .//th[contains(normalize-space(.), '产品') or contains(normalize-space(.), '品名')]
          and .//th[contains(normalize-space(.), '规格')]
          and (
            .//th[
              contains(normalize-space(.), '平均')
              or contains(normalize-space(.), '均价')
              or contains(normalize-space(.), '价格')
            ]
          )
        ]
        """
    )

    # 如未命中，则回退到所有表格
    if not candidate_tables:
        candidate_tables = doc.xpath("//table")

    for table in candidate_tables:
        # 提取表头，用于列索引映射（若存在）
        header_cells = table.xpath(".//tr[.//th][1]//th")
        name_idx = spec_idx = avg_idx = date_idx = None
        if header_cells:
            header_texts = [_text(th) for th in header_cells]
            for i, ht in enumerate(header_texts):
                if name_idx is None and ("产品" in ht or "品名" in ht or "名称" in ht):
                    name_idx = i
                if spec_idx is None and ("规格" in ht or "等级" in ht):
                    spec_idx = i
                if avg_idx is None and ("平均" in ht or "均价" in ht or "价格" in ht):
                    avg_idx = i
                if date_idx is None and ("日期" in ht or re.search(r"[\u65e5\u671f]", ht)):
                    date_idx = i

        # 遍历数据行（排除含 th 的行）
        data_rows = table.xpath(".//tr[not(.//th) and count(.//td) >= 4]")
        for tr in data_rows:
            tds = tr.xpath("./td")
            if len(tds) < 4:
                continue

            # 若无法从表头确定索引，则默认按常规顺序取前四列
            i_name = name_idx if name_idx is not None and name_idx < len(tds) else 0
            i_spec = spec_idx if spec_idx is not None and spec_idx < len(tds) else 1
            i_avg = avg_idx if avg_idx is not None and avg_idx < len(tds) else 2
            i_date = date_idx if date_idx is not None and date_idx < len(tds) else 3

            product_name = _text(tds[i_name])
            specification = _text(tds[i_spec])
            average_price = _text(tds[i_avg])
            date_value = _text(tds[i_date])

            # 基本校验
            if not product_name:
                continue
            if not re.search(r"\d", average_price):
                continue
            if not re.search(r"[-./]", date_value):
                # 尝试后移一列
                if i_date + 1 < len(tds):
                    candidate_date = _text(tds[i_date + 1])
                    if re.search(r"[-./]", candidate_date):
                        date_value = candidate_date
                    else:
                        continue
                else:
                    continue

            results.append((product_name, specification, average_price, date_value))

    return results


def iter_pages(start: int, end: int) -> Iterable[int]:
    step = 1 if end >= start else -1
    for p in range(start, end + step, step):
        yield p


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


if __name__ == "__main__":
    main()


