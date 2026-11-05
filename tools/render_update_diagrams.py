"""Render the update guide's PNG flowcharts without a Mermaid viewer.

Run with Python and Pillow. No network access or application changes are needed.
The original Mermaid sources are kept beside the generated images.
"""
from __future__ import annotations

from pathlib import Path
import math
import re

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "上位机远程更新实现与故障修复说明.md"
OUTPUT = ROOT / "docs" / "images" / "auto_update"
REGULAR = Path("C:/Windows/Fonts/msyh.ttc")
BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")
PALETTE = {
    "blue": ("#EDF4FF", "#91B6E7", "#224D80"),
    "teal": ("#EAF7F3", "#8DC7B3", "#155E4A"),
    "amber": ("#FFF7E9", "#DEBE7E", "#75501A"),
    "red": ("#FFF1F0", "#E0A5A0", "#873B35"),
    "purple": ("#F4F0FC", "#BDA7DD", "#5B4383"),
}


class Flowchart:
    def __init__(self, width, height, title, subtitle):
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.draw.rectangle((0, 0, width, 145), fill="#F0F5FA")
        self.draw.rounded_rectangle((54, 44, 65, 106), radius=5, fill="#18806B")
        self.draw.text((88, 34), title, font=self.font(49, True), fill="#18364D")
        self.draw.text((90, 101), subtitle, font=self.font(25), fill="#52677A")

    @staticmethod
    def font(size, bold=False):
        return ImageFont.truetype(str(BOLD if bold else REGULAR), size)

    def wrap(self, text, font, width):
        lines = []
        for paragraph in text.splitlines():
            line = ""
            for character in paragraph:
                candidate = line + character
                if line and self.draw.textlength(candidate, font=font) > width:
                    lines.append(line)
                    line = character
                else:
                    line = candidate
            lines.append(line)
        return lines

    def node(self, cx, top, width, height, title, subtitle="", kind="blue",
             diamond=False, number=None):
        left, right, bottom = cx-width//2, cx+width//2, top+height
        fill, border, ink = PALETTE[kind]
        if diamond:
            points = [(cx, top), (right, top+height//2),
                      (cx, bottom), (left, top+height//2)]
            self.draw.polygon(points, fill=fill)
            self.draw.line(points+[points[0]], fill=border, width=3)
        else:
            self.draw.rounded_rectangle((left, top, right, bottom),
                                        radius=18, fill=fill, outline=border, width=3)
        available = int(width*.68) if diamond else width-64
        for size in range(35, 23, -1):
            title_font, sub_font = self.font(size, True), self.font(size-7)
            title_lines = self.wrap(title, title_font, available)
            sub_lines = self.wrap(subtitle, sub_font, available) if subtitle else []
            line_height = size+12
            sub_height = size+6
            block_height = len(title_lines)*line_height + len(sub_lines)*sub_height
            if sub_lines:
                block_height += 5
            if block_height <= height-24:
                break
        if block_height > height-12:
            raise ValueError("Flowchart label exceeds node: " + title)
        y = top+(height-block_height)/2
        for line in title_lines:
            self.draw.text((cx, y), line, anchor="mt", font=title_font, fill=ink)
            y += line_height
        if sub_lines:
            y += 5
        for line in sub_lines:
            self.draw.text((cx, y), line, anchor="mt", font=sub_font, fill=ink)
            y += sub_height
        if number is not None:
            badge_x, badge_y = left-49, top+height//2
            self.draw.ellipse((badge_x-26, badge_y-26, badge_x+26, badge_y+26),
                              fill=ink)
            self.draw.text((badge_x, badge_y-1), str(number), anchor="mm",
                           font=self.font(26, True), fill="white")

    def arrow(self, points, label="", label_at=None, color="#5A7083"):
        self.draw.line(points, fill=color, width=4, joint="curve")
        start, end = points[-2:]
        angle = math.atan2(end[1]-start[1], end[0]-start[0])
        back_x, back_y = end[0]-17*math.cos(angle), end[1]-17*math.sin(angle)
        perpendicular_x, perpendicular_y = -8*math.sin(angle), 8*math.cos(angle)
        self.draw.polygon([end, (back_x+perpendicular_x, back_y+perpendicular_y),
                           (back_x-perpendicular_x, back_y-perpendicular_y)], fill=color)
        if label:
            label_font = self.font(25, True)
            box = self.draw.textbbox(label_at, label, anchor="mm", font=label_font)
            self.draw.rounded_rectangle((box[0]-9, box[1]-5, box[2]+9, box[3]+5),
                                        radius=5, fill="white")
            self.draw.text(label_at, label, anchor="mm", font=label_font, fill=color)

    def footer(self, text):
        self.draw.line((60, self.height-102, self.width-60, self.height-102),
                       fill="#DFE7EF", width=2)
        for index, line in enumerate(self.wrap(text, self.font(24), self.width-140)):
            self.draw.text((70, self.height-84+index*34), line,
                           font=self.font(24), fill="#52677A")

    def save(self, name):
        path = OUTPUT / name
        self.image.save(path, optimize=True, dpi=(144, 144))
        print(f"{path.relative_to(ROOT)}: {self.width} x {self.height}")


def automatic_update():
    chart = Flowchart(1820, 2810, "上位机自动更新流程",
                      "后台检查与续传 → 空闲切换 → 新版本试启动确认（v2.10.5）")
    x, w = 770, 780
    chart.node(x, 190, w, 100, "运行顶层 RTTTool.exe", "获取当前用户会话锁")
    chart.arrow([(x, 290), (x, 350)])
    chart.node(x, 350, w, 140, "存在已准备更新？", diamond=True)
    chart.arrow([(1160, 420), (1760, 420), (1760, 2045), (1160, 2045)],
                "有：先安装", (1440, 420))
    chart.arrow([(x, 490), (x, 550)], "无", (x+40, 520))
    chart.node(x, 550, w, 100, "启动当前 GUI", "启动约 3 秒后触发后台检查")
    chart.arrow([(x, 650), (x, 710)])
    chart.node(x, 710, w, 110, "后台读取签名清单", "GitHub API 首选；网络故障时尝试备用入口")
    chart.arrow([(x, 820), (x, 880)])
    chart.node(x, 880, w, 150, "清单可信且发现新版？", diamond=True)
    chart.arrow([(1160, 955), (1280, 955)], "否／失败", (1220, 912))
    chart.node(1490, 875, 420, 160, "继续使用当前版本",
               "没有新版：正常使用\n网络故障：记录后重试", kind="amber")
    chart.arrow([(x, 1030), (x, 1090)], "是", (x+40, 1060))
    chart.node(x, 1090, w, 110, "下载或续传应用 ZIP", "以实际收到的字节显示百分比、大小与速度")
    chart.arrow([(x, 1200), (x, 1260)])
    chart.node(x, 1260, w, 150, "完整大小、SHA-256 一致？", diamond=True)
    chart.arrow([(1160, 1335), (1280, 1335)], "否／中断", (1220, 1292))
    chart.node(1490, 1255, 420, 160, "继续使用当前版本",
               "网络中断：保存进度重试\n摘要错误：丢弃并拒绝安装", kind="amber")
    chart.arrow([(x, 1410), (x, 1470)], "是", (x+40, 1440))
    chart.node(x, 1470, w, 110, "原子写入 pending.json", "只把完整、可信的更新交给启动器", kind="teal")
    chart.arrow([(x, 1580), (x, 1640)])
    chart.node(x, 1640, w, 110, "等待设备任务结束及连续空闲 60 秒",
               "也可手动重启更新，或在下次启动安装", kind="teal")
    chart.arrow([(x, 1750), (x, 1810)])
    chart.node(x, 1810, w, 100, "GUI 正常退出", "返回更新退出码 42", kind="teal")
    chart.arrow([(x, 1910), (x, 1990)])
    chart.node(x, 1990, w, 110, "启动器安装到独立版本目录",
               "再次验签，校验完整包、ZIP 路径和版本信息", kind="purple")
    chart.arrow([(x, 2100), (x, 2160)])
    chart.node(x, 2160, w, 110, "试运行新版 GUI",
               "加载关键组件，写入本次版本与 nonce 的健康回执", kind="purple")
    chart.arrow([(x, 2270), (x, 2330)])
    chart.node(x, 2330, w, 150, "45 秒内收到有效回执？", kind="purple", diamond=True)
    chart.arrow([(x, 2480), (x, 2550)], "是", (x+40, 2515))
    chart.node(x, 2550, w, 110, "提交新版，更新成功", "解除试启动状态；沿用原有配置", kind="teal")
    chart.arrow([(1160, 2405), (1490, 2405), (1490, 2550)],
                "否／启动失败", (1390, 2405))
    chart.node(1490, 2550, 420, 110, "恢复上一版", "记录并跳过本次失败版本", kind="red")
    chart.footer("下载和校验失败时继续运行当前版本；设备任务、连接或模态窗口存在时，等待任务结束再自动切换。")
    chart.save("auto_update_flow.png")


def recovery():
    chart = Flowchart(1460, 1770, "旧版更新失败后的修复流程",
                      "修复工具负责下载准备，原顶层启动器负责安装与试运行")
    steps = [
        ("退出原上位机", "先等待设备任务结束"),
        ("运行新版修复工具", "完整解压，保留 _internal 目录"),
        ("选择原目录顶层 RTTTool.exe", "保留原启动器、更新配置和用户资料"),
        ("获取会话锁并识别原应用版本", "按原应用版本判断更新，不使用工具自身版本"),
        ("读取远程清单并验签", "检查来源、版本、有效期和启动器兼容性"),
        ("下载或续传，校验完整应用包", "检查完整大小和 SHA-256"),
        ("准备完成：写入 pending.json", "此时尚未替换正在运行的应用"),
        ("点击“启动上位机”", "运行原目录顶层 RTTTool.exe"),
        ("原启动器安装并试运行新版", "健康检查通过后提交新版，沿用原配置"),
    ]
    for index, (title, subtitle) in enumerate(steps):
        y = 190+index*160
        chart.node(780, y, 1110, 112, title, subtitle,
                   kind="blue" if index < 6 else "teal", number=index+1)
        if index < len(steps)-1:
            chart.arrow([(780, y+112), (780, y+160)])
    chart.footer("若仍失败：运行“HTTPS 诊断”并复制诊断文本；连接探测通过后，仍需完成完整包下载与校验。")
    chart.save("update_repair_flow.png")


def publication():
    chart = Flowchart(1460, 2250, "远程更新版本发布流程",
                      "先公开并验证附件，再同步签名清单，最后切换正式 Latest")
    steps = [
        ("修改真实应用版本并完成回归", "确认 APP_VERSION、功能与兼容性"),
        ("构建基础便携包及修复工具", "更新包包含完整应用和运行依赖"),
        ("使用原发布私钥签发清单", "清单绑定版本、URL、大小、摘要与有效期"),
        ("验证签名、ZIP 和应用启动", "核对依赖、许可证以及原生组件加载"),
        ("创建新版本 Release 草稿", "上传基础包、应用包、latest.json 和修复工具"),
        ("核对服务端附件大小及 SHA-256", "上传完成后再进入公开验证"),
        ("公开该版本的固定下载地址", "暂不切换正式 Latest"),
        ("验证公开下载与清单签名", "确保客户端下载到的是已验证的那份包"),
        ("同步 updates/stable.json 并推送", "仓库镜像保留同一份签名清单"),
        ("确认 GitHub API 返回同一清单", "核对目标版本、摘要与签名"),
        ("将 Release 设为正式 Latest", "正式备用入口与 API 入口指向同一版本"),
        ("用旧版完成公开网络升级验证", "检查下载、续传、试启动及配置保留"),
    ]
    for index, (title, subtitle) in enumerate(steps):
        y = 190+index*160
        kind = "blue" if index < 4 else "purple" if index < 8 else "teal"
        chart.node(780, y, 1110, 112, title, subtitle, kind=kind, number=index+1)
        if index < len(steps)-1:
            chart.arrow([(780, y+112), (780, y+160)])
    chart.footer("不要覆盖已公开版本的应用包；正式发布与测试 Pre-release 使用各自清单，测试不切换正式 Latest。")
    chart.save("update_publish_flow.png")


def main():
    if not REGULAR.is_file() or not BOLD.is_file():
        raise SystemExit("Microsoft YaHei font files were not found. Set REGULAR and BOLD first.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    names = ["auto_update_flow", "update_repair_flow", "update_publish_flow"]
    # Preserve the diagrams from the guide on the first export. Later runs only
    # redraw the PNGs; the sources remain available for readers who use Mermaid.
    sources = re.findall(r"```mermaid\s*\n(.*?)\n```", GUIDE.read_text(encoding="utf-8"), re.S)
    if sources:
        if len(sources) != len(names):
            raise SystemExit("Expected three Mermaid diagrams in the update guide.")
        for name, source in zip(names, sources):
            (OUTPUT/(name+".mmd")).write_text(source+"\n", encoding="utf-8")
    automatic_update()
    recovery()
    publication()


if __name__ == "__main__":
    main()
