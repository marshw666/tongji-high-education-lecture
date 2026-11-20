#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同济高等讲堂（https://lecture.tongji.edu.cn）报名命令行工具

设计要点
--------
* 认证只认 `token` 请求头（站点前端从 cookie `token` / URL 参数 `token` 取，再塞进 header）。
  "学号 / user-id" 只是业务字段，不能用来鉴权。
* 所有 POST 接口都是 multipart/form-data（前端用 FormData），这里保持一致。
* 真正提交报名的是 `SginInLectureHallCredentials`（注意官方拼写就是 Sgin），
  它需要一个由 `SetCredentials` 现签的短时效 `code`（防脚本重放），所以每次尝试前都要重新签。
* 服务端时间是唯一可信的基准，这里用 HTTP `Date` 头做时钟校准。

用法
----
    export TJH_TOKEN='粘贴你的 token'
    python3 tjh.py whoami
    python3 tjh.py list --status 3          # 未开始报名的讲座
    python3 tjh.py detail 12345
    python3 tjh.py signup 12345             # 按讲座的 TimeSigningUp 卡点报名
    python3 tjh.py signup 12345 --now       # 立刻尝试一次（不等待）
    python3 tjh.py cancel 12345

token 也可用 --token 传入，或写在同目录 token.txt 里。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

BASE = "https://lecture.tongji.edu.cn"
CONFIG_JS = BASE + "/static/js/config.js"

# config.js 里的网关 Basic 头（客户端凭证，不是用户身份；若学校轮换会自动重新抓取）
FALLBACK_AUTH = (
    "Basic cTN4YXRwbTV0eHc0MmtoejVjYzg1YWY0OmI1dTRteWVrZDVkYno0cGUxcTFoY3dnb3h4eXR0bTM3"
)

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40 NetType/WIFI Language/zh_CN"
)

CN_TZ = timezone(timedelta(hours=8))

# LectureHallStatus 语义（来自前端 LectureItem 组件）
STATUS_TEXT = {
    1: "有余位（报名中）",
    2: "已报满",
    3: "未开始报名",
    4: "已结束",
}

# 服务端 msg 的粗分类，用来决定"继续重试"还是"直接放弃"
FULL_WORDS = ("已满", "报满", "名额", "人数已达", "超出")
ENDED_WORDS = ("已结束", "已截止", "结束报名")
NOT_START_WORDS = ("未开始", "未到", "还没开始", "尚未开始", "不在报名时间")
DONE_WORDS = ("已报名", "重复", "已经报名", "请勿重复")
LIST_WORDS = ("名单", "无报名资格", "不具备")


PAGE_SIZE_MAX = 50
CREDENTIAL_STATUS_TEXT = {
    7: "讲座已下线或不存在",
    8: "报名排队中",
    9: "已报名",
    10: "名额已满",
    11: "名额释放中",
}


class ApiError(RuntimeError):
    pass


class TokenExpired(ApiError):
    pass


class SignupUnavailable(ApiError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def parse_time(value: Any) -> datetime | None:
    """解析后端返回的时间（通常是 '2026-11-06T12:00:00' 这种无时区字符串，按北京时间理解）。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, CN_TZ).strftime("%H:%M:%S.%f")[:-3]


def has_signup_time(value: Any) -> bool:
    return isinstance(value, str) and parse_time(value) is not None


class TjLecture:
    def __init__(
        self,
        token: str = "",
        cookie: str = "",
        ua: str = "",
        timeout: float = 20.0,
        verbose: bool = False,
    ):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.verbose = verbose
        self.offset = 0.0  # 服务端时间 - 本地时间
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": BASE + "/wxpage/index",
                "Origin": BASE,
            }
        )
        if ua:
            self.session.headers["User-Agent"] = ua
        if cookie:
            self._load_cookie(cookie)
        self.auth = self._load_auth()

    def _load_cookie(self, raw: str) -> None:
        """把浏览器里复制的一整串 cookie（k=v; k2=v2）灌进 session。"""
        count = 0
        for part in raw.replace("\n", ";").split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            self.session.cookies.set(name, value, domain="lecture.tongji.edu.cn", path="/")
            count += 1
        if self.verbose:
            print(f"已载入 {count} 个 cookie", file=sys.stderr)

    # ---------------------------------------------------------------- 基础设施

    def _load_auth(self) -> str:
        """从 config.js 读取网关 Basic 头，失败则用内置值。"""
        try:
            resp = self.session.get(CONFIG_JS, timeout=self.timeout)
            match = re.search(r'Authorization\s*:\s*"([^"]+)"', resp.text)
            if match:
                return match.group(1)
        except requests.RequestException:
            pass
        return FALLBACK_AUTH

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": self.auth}
        if self.token:
            headers["token"] = self.token
        return headers

    @property
    def has_auth(self) -> bool:
        return bool(self.token) or len(self.session.cookies) > 0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = BASE + path
        files = None
        data = None
        if form is not None:
            # 与前端 FormData 一致：multipart/form-data
            files = {key: (None, str(value)) for key, value in form.items()}
        elif params is not None:
            data = None

        if self.verbose:
            print(f"→ {method} {path} params={params} form={form}", file=sys.stderr)

        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                data=data,
                files=files,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"网络错误：{exc}") from exc

        if resp.status_code == 401:
            raise TokenExpired("登录已失效，请重新登录并更新登录凭证。")

        if resp.status_code >= 500:
            hint = (
                "没有可用凭证（token 和 cookie 都没带）"
                if not self.has_auth
                else "token/cookie 无效或已过期（服务端对未认证请求直接 500 且无响应体）"
            )
            raise TokenExpired(f"HTTP {resp.status_code}：{hint}")

        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"返回不是 JSON：{resp.text[:300]}") from exc

        if isinstance(payload, dict) and payload.get("code") in (40001, "nologin"):
            raise TokenExpired("登录已失效，请重新登录并更新登录凭证。")

        return payload

    # ------------------------------------------------------------------ 时钟同步

    def sync_time(self) -> float:
        """用 HTTP Date 头估算服务端时间偏移（取请求往返中点）。"""
        t0 = time.time()
        try:
            resp = self.session.head(BASE + "/", timeout=self.timeout)
            t1 = time.time()
            date_header = resp.headers.get("Date")
            if date_header:
                server_ts = parsedate_to_datetime(date_header).timestamp()
                self.offset = server_ts - (t0 + t1) / 2.0
        except Exception:
            self.offset = 0.0
        return self.offset

    def now(self) -> float:
        return time.time() + self.offset

    # --------------------------------------------------------------------- 接口

    def doctor(self) -> int:
        """自检：打印实际发出的请求头和返回，用来定位 500/401 的原因。"""
        names = [c.name for c in self.session.cookies]
        print("=== 凭证 ===")
        print(f"  token        : {'已设置 ' + self.token[:10] + '...' if self.token else '未设置'}")
        print(f"  cookie 数量  : {len(names)}")
        for c in self.session.cookies:
            value = c.value or ""
            print(f"    - {c.name} = {value[:12]}...（{len(value)} 字符）")
        print(f"  User-Agent   : {self.session.headers.get('User-Agent')}")
        print(f"  Authorization: {self.auth[:24]}...")
        print()

        url = BASE + "/api/gj/studentwx/studentapi/StudentInfo"
        print(f"=== 试请求 {url} ===")
        try:
            resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            print(f"  × 网络错误：{exc}")
            return 1

        print(f"  HTTP {resp.status_code}")
        print(f"  实际发送的 Cookie: {resp.request.headers.get('Cookie') or '(无)'}")
        print(f"  实际发送的 token : {resp.request.headers.get('token') or '(无)'}")
        body = resp.text[:400]
        print(f"  响应体: {body or '(空)'}")
        print()

        if resp.status_code == 200 and body.strip().startswith("{"):
            print("√ 认证成功，可以正常使用。")
            return 0
        if resp.status_code == 500 and not body.strip():
            print("× 服务端认为你没登录（500 空响应）。常见原因：")
            print("   1. cookie 过期或复制不完整（少了分号、被换行截断）")
            print("   2. 只复制了 PG.ASP.WeiXinCode.ID，漏了 PG.ASP.Session.ID")
            print("   3. 换了浏览器/无痕窗口，session 对不上")
            print("   4. 会话绑定了 UA，加 --ua '<浏览器里那条 User-Agent>' 再试")
            return 1
        print("× 非预期响应，把上面的信息发出来。")
        return 1

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/api/gj/studentwx/studentapi/StudentInfo")

    def face_ok(self) -> bool:
        payload = self._request("GET", "/api/gj/studentwx/studentapi/GetIsFinishFace")
        return payload.get("state") == "success" and payload.get("data") is True

    def list_lectures(
        self,
        page: int = 1,
        page_size: int = 20,
        status: int = 0,
        time_filter: int = 0,
        class_level: int = 0,
    ) -> dict[str, Any]:
        if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE_MAX:
            raise ApiError(f"每页条目数必须是 1–{PAGE_SIZE_MAX} 之间的整数。")
        return self._request(
            "GET",
            "/api/gj/studentwx/lecturehallapi/GetLectureHallList",
            params={
                "page": page,
                "pageSize": page_size,
                "status": status,
                "time": time_filter,
                "classLevel": class_level,
            },
        )

    def detail(self, data_id: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/gj/studentwx/lecturehallapi/GetLectureHall",
            params={"DataId": data_id},
        )
        if payload.get("state") != "success" or not payload.get("data"):
            raise ApiError(f"获取讲座详情失败：{payload.get('msg') or payload}")
        return payload["data"]

    def set_credentials(self, data_id: str) -> str:
        """取得学校当前允许用于该讲座报名的凭证。"""
        payload = self._request(
            "POST", "/api/gj/studentwx/studentapi/SetCredentials", form={"dataId": data_id}
        )
        if payload.get("state") != "success":
            raise SignupUnavailable(None, payload.get("msg") or "获取报名资格码失败。")
        data = payload.get("data")
        status = data.get("status") if isinstance(data, dict) else None
        if type(status) is not int:
            raise SignupUnavailable(None, "学校未返回可识别的报名状态，停止提交。")
        if status != 0:
            label = CREDENTIAL_STATUS_TEXT.get(status) or data.get("message") or f"暂无报名资格（状态 {status}）"
            raise SignupUnavailable(status, label)
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ApiError("学校未返回有效的报名资格码。")
        return code

    def check_credentials(self, code: str) -> bool:
        payload = self._request(
            "POST", "/api/gj/studentwx/studentapi/GetCredentials", form={"code": code}
        )
        return payload.get("state") == "success" and (payload.get("data") or {}).get("iscode") is True

    def signup_with_code(self, code: str, data_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/gj/studentwx/studentapi/SginInLectureHallCredentials",
            form={"code": code, "dataId": data_id},
        )

    def signup_result(self, data_id: str) -> dict[str, Any]:
        payload = self._request(
            "POST", "/api/gj/studentwx/studentapi/GetSginInTime", form={"dataId": data_id}
        )
        if payload.get("state") != "success":
            raise ApiError(payload.get("msg") or "学校未能返回报名结果。")
        return payload.get("data") or {}

    def check(self, data_id: str | None = None) -> int:
        """资格自检：账号层面（登录/名单/人脸）+ 讲座层面（状态/是否已报）。"""
        print("=== 资格自检 ===")
        ok = True

        try:
            me = self.whoami().get("data") or {}
        except TokenExpired:
            raise
        except ApiError as exc:
            print(f"[✗] 登录态        失败：{exc}")
            return 2

        if not me.get("StudentId"):
            print("[✗] 登录态        拿不到用户信息")
            return 2

        print(f"[✓] 登录态        {me.get('Name')} / {me.get('StudentId')}")

        membership = me.get("IsInStudentList")
        if membership is True:
            print("[✓] 在可报名名单   是")
        elif membership == "null" and data_id:
            print("[!] 名单状态未明确，报名时以该场讲座的资格状态为准")
        else:
            print("[✗] 名单字段未通过或无法核实")
            return 2

        face = me.get("IsFinishPhoto") in ("1", 1, True)
        print(f"[{'✓' if face else '✗'}] 人像采集       {'已完成' if face else '未完成（报名按钮是灰的）'}")
        ok = ok and face

        try:
            face_api = self.face_ok()
            print(f"[{'✓' if face_api else '✗'}] 人脸校验接口   {'通过' if face_api else '未通过'}")
            ok = ok and face_api
        except ApiError as exc:
            print(f"[!] 人脸校验接口   查询失败：{exc}")
            return 2

        if data_id:
            try:
                detail = self.detail(data_id)
            except TokenExpired:
                raise
            except ApiError as exc:
                print(f"[✗] 讲座           获取失败：{exc}")
                return 2

            status = detail.get("LectureHallStatus")
            open_at = detail.get("TimeSigningUp")
            already = detail.get("XSSigningUp")

            print(f"[ ] 讲座           {detail.get('Cathedra')}")
            print(f"[{'✓' if status == 1 else '·'}] 报名状态       {status} {STATUS_TEXT.get(status, '未知')}")
            print(f"[ ] 报名开放       {open_at or '无'}")
            print(f"[{'!' if already else '✓'}] 是否已报名     {already or '未报名'}")

            if has_signup_time(already):
                print("[✓] 结论           已经报过了")
                return 0
            if status == 2:
                print("[✗] 结论           已报满，等别人取消或换一场")
                return 1
            if status == 4:
                print("[✗] 结论           已结束")
                return 1
            if status == 1:
                print("[✓] 结论           现在就能报：python3 tjh.py signup " + str(data_id))
                return 0 if ok else 1
            if status == 3:
                print(f"[⏳] 结论           未到开放时间（{open_at}），可用 signup 卡点")
                return 0 if ok else 1
            print("[?] 结论           状态未知，建议 tjh.py detail 看原始字段")
            return 1

        print()
        print("√ 账号资格正常" if ok else "× 账号资格有问题，见上面标 ✗ 的项")
        return 0 if ok else 1

    def cancel(self, data_id: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/gj/studentwx/studentapi/CancelLectureHall", form={"dataId": data_id}
        )

    # ----------------------------------------------------------------- 业务编排

    def wait_signup_result(self, data_id: str, seconds: float = 60.0) -> tuple[bool | None, str]:
        """报名提交后轮询结果：state 1 成功 / -1 失败 / 其它 处理中。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data = self.signup_result(data_id)
            except ApiError as exc:
                return None, f"查询结果出错：{exc}"
            state = data.get("state")
            if state == 1 and has_signup_time(data.get("signUpTime")):
                when = data.get("signUpTime")
                return True, f"报名成功（服务端时间 {when}）"
            if state == -1:
                return False, "报名失败（服务端判定）"
            time.sleep(0.6)
        return None, "结果未知：仍在处理中，可稍后在“我的讲座”查看"

    def confirm_signup(self, data_id: str) -> int:
        ok, message = self.wait_signup_result(data_id)
        print(("√ " if ok is True else "× " if ok is False else "? ") + message)
        return 0 if ok else 1

    def confirm_existing_signup(self, data_id: str) -> int:
        try:
            recorded_time = self.detail(data_id).get("XSSigningUp")
        except ApiError:
            recorded_time = None
        if has_signup_time(recorded_time):
            print(f"√ 已从讲座详情核实报名记录（{recorded_time}）。")
            return 0
        print("? 服务端提示已报名，详情中尚未核实。请到“我的讲座”查看。")
        return 1

    def signup(
        self,
        data_id: str,
        *,
        at: datetime | None = None,
        lead: float = 0.4,
        patience: float = 180.0,
        interval: float = 0.25,
        once: bool = False,
        dry_run: bool = False,
    ) -> int:
        detail = self.detail(data_id)
        title = detail.get("Cathedra") or detail.get("LectureSynopsis") or ""
        status = detail.get("LectureHallStatus")
        open_at = parse_time(detail.get("TimeSigningUp"))
        already = detail.get("XSSigningUp")

        print(f"讲座：{title}")
        print(f"  DataId      : {data_id}")
        print(f"  状态        : {status} {STATUS_TEXT.get(status, '未知')}")
        print(f"  讲座时间    : {detail.get('LectureTimeStart')}")
        print(f"  报名开放    : {detail.get('TimeSigningUp')}")
        print(f"  已报名时间  : {already or '未报名'}")

        if has_signup_time(already):
            print("你已经报过这场了，无需重复操作。")
            return 0

        try:
            me = self.whoami().get("data") or {}
            print(f"  当前身份    : {me.get('Name')} / {me.get('StudentId')}")
            membership = me.get("IsInStudentList")
            if not me.get("StudentId") or (membership is not True and membership != "null"):
                print("× 登录身份或报名名单未通过核验。")
                return 2
            if membership == "null":
                print("  名单状态未明确，报名时以该场讲座的资格状态为准。")
        except TokenExpired:
            raise
        except ApiError as exc:
            print(f"  （读取身份失败，停止：{exc}）")
            return 2

        try:
            if not self.face_ok():
                print("× 未完成人脸采集（GetIsFinishFace=false），页面也会禁用报名按钮。")
                print("  需要先在微信端/一体机完成人像采集。")
                return 2
        except ApiError as exc:
            print(f"  （人脸状态检查失败，停止：{exc}）")
            return 2

        if dry_run:
            print("dry-run：以上检查通过，未提交报名。")
            return 0

        self.sync_time()
        if once:
            target = self.now()
        else:
            if not at:
                at = open_at
            if not at and status == 1:
                # 已经在报名中、又没有开放时间字段：直接开抢
                at = _now_cn()
            if not at:
                print("× 拿不到报名开放时间，请用 --at 手动指定，或加 --now 立即尝试。")
                return 2
            target = at.timestamp() - lead
            wait = target - self.now()
            print(f"  目标时刻    : {at.strftime('%Y-%m-%d %H:%M:%S')}（服务端时钟）")
            if wait > 0:
                print(f"  等待 {wait:.1f}s ...")
                while True:
                    remain = target - self.now()
                    if remain <= 0:
                        break
                    time.sleep(min(remain, 30.0))

        deadline = self.now() + (patience if not once else 15.0)
        attempt = 0
        while self.now() < deadline:
            attempt += 1
            try:
                code = self.set_credentials(data_id)
            except SignupUnavailable as exc:
                print(f"[{fmt_ts(self.now())}] #{attempt} {exc}")
                if exc.status == 8:
                    return self.confirm_signup(data_id)
                if exc.status == 9:
                    return self.confirm_existing_signup(data_id)
                if not once and at and exc.status not in CREDENTIAL_STATUS_TEXT:
                    remaining = at.timestamp() - self.now()
                    if remaining > 0 and any(word in str(exc) for word in NOT_START_WORDS):
                        time.sleep(remaining)
                        continue
                return 1
            except TokenExpired:
                raise
            except ApiError as exc:
                print(f"× 获取报名资格码失败，停止：{exc}")
                return 1

            try:
                resp = self.signup_with_code(code, data_id)
            except TokenExpired:
                raise
            except ApiError as exc:
                print(f"[{fmt_ts(self.now())}] #{attempt} 请求异常：{exc}")
                print("提交结果不明确，停止重试，请在“我的讲座”核对。")
                return 1

            state = resp.get("state")
            msg = str(resp.get("msg") or "")

            if state == "success":
                print(f"[{fmt_ts(self.now())}] #{attempt} 已受理，查询结果 ...")
                return self.confirm_signup(data_id)

            print(f"[{fmt_ts(self.now())}] #{attempt} 未成功：{msg or resp}")

            if any(word in msg for word in DONE_WORDS):
                return self.confirm_existing_signup(data_id)
            if any(word in msg for word in LIST_WORDS):
                print("× 无报名资格，停止。")
                return 2
            if any(word in msg for word in FULL_WORDS):
                print("× 名额已满，停止。")
                return 1
            if any(word in msg for word in ENDED_WORDS):
                print("× 报名已结束，停止。")
                return 1
            if any(word in msg for word in NOT_START_WORDS):
                if once:
                    return 1
                time.sleep(interval + random.uniform(0, 0.08))
                continue

            print("? 服务端响应不明确，停止重试，请在“我的讲座”核对。")
            return 1

        print("× 超时未成功。")
        return 1


# ------------------------------------------------------------------------ 输出


def dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def print_list(payload: dict[str, Any]) -> None:
    data = payload.get("data") or {}
    rows = data.get("row") or []
    if not rows:
        print("（没有数据）")
        return
    print(f"{'DataId':<12} {'状态':<14} {'报名开放':<20} {'讲座时间':<17} 标题")
    print("-" * 100)
    for row in rows:
        status = row.get("LectureHallStatus")
        print(
            f"{str(row.get('DataId')):<12} "
            f"{STATUS_TEXT.get(status, str(status)):<14} "
            f"{str(row.get('TimeSigningUp') or '-'):<20} "
            f"{str(row.get('LectureTimeStart') or '-'):<17} "
            f"{row.get('Cathedra') or ''}"
        )
    total = data.get("totalPage")
    if total:
        print(f"\n共 {total} 页，当前第 {data.get('page')} 页")


# -------------------------------------------------------------------------- CLI


def _read_file(name: str) -> str:
    path = Path(__file__).with_name(name)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def resolve_auth(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (token, cookie)。两者有其一即可。"""
    token = (getattr(args, "token", None) or "").strip()
    if not token:
        token = os.environ.get("TJH_TOKEN", "").strip() or _read_file("token.txt")

    cookie = (getattr(args, "cookie", None) or "").strip()
    if not cookie:
        cookie = os.environ.get("TJH_COOKIE", "").strip() or _read_file("cookie.txt")

    return token, cookie


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同济高等讲堂报名工具（token 鉴权）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--token", help="登录后的 token（也可用环境变量 TJH_TOKEN / token.txt）")
    parser.add_argument(
        "--ua",
        help="自定义 User-Agent（默认手机微信 UA；某些会话会校验 UA 时用）",
    )
    parser.add_argument(
        "--cookie",
        help="浏览器里复制的整串 Cookie（也可用 TJH_COOKIE / cookie.txt）。"
        "token 读不到时用这个，例如 'token=xxx; PG.ASP.Session.ID=yyy'",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印请求细节")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="查看当前登录身份")
    sub.add_parser("doctor", help="自检：打印请求头/响应，排查 500")

    p_check = sub.add_parser("check", help="资格自检：能不能报名")
    p_check.add_argument("data_id", nargs="?", help="讲座 DataId；不填只查账号资格")

    p_list = sub.add_parser("list", help="列出讲座")
    p_list.add_argument("--status", type=int, default=0, choices=[0, 1, 2, 3, 4],
                        help="0全部 1有余位 2已报满 3未开始 4已结束")
    p_list.add_argument("--time", type=int, default=0, choices=[0, 1, 2],
                        help="0全部 1当月 2往期")
    p_list.add_argument("--level", type=int, default=0, choices=[0, 1, 2],
                        help="0全部 1校级 2院级")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=20,
                        help=f"每页条目数（1–{PAGE_SIZE_MAX}，默认 20）")
    p_list.add_argument("--json", action="store_true", help="输出原始 JSON")

    p_detail = sub.add_parser("detail", help="查看讲座详情")
    p_detail.add_argument("data_id")

    p_sign = sub.add_parser("signup", help="报名（默认按报名开放时间卡点）")
    p_sign.add_argument("data_id")
    p_sign.add_argument("--at", help="指定开抢时刻，如 '2026-11-06 12:00:00'（北京时间）")
    p_sign.add_argument("--now", action="store_true", help="不等待，立即尝试")
    p_sign.add_argument("--lead", type=float, default=0.4, help="提前多少秒开始尝试（默认 0.4）")
    p_sign.add_argument("--patience", type=float, default=180.0, help="开抢后最多坚持多少秒")
    p_sign.add_argument("--interval", type=float, default=0.25, help="每次尝试的间隔秒数")
    p_sign.add_argument("--dry-run", action="store_true", help="只做检查不提交")

    p_cancel = sub.add_parser("cancel", help="取消报名")
    p_cancel.add_argument("data_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token, cookie = resolve_auth(args)
    if not token and not cookie:
        print("× 缺少凭证。任选一种：", file=sys.stderr)
        print("    --token '...'    或 export TJH_TOKEN='...'    或 token.txt", file=sys.stderr)
        print("    --cookie '...'   或 export TJH_COOKIE='...'   或 cookie.txt", file=sys.stderr)
        print("  获取方法见 README.md 的「拿到 token / cookie」一节。", file=sys.stderr)
        return 2

    api = TjLecture(token=token, cookie=cookie, ua=args.ua or "", verbose=args.verbose)

    try:
        if args.cmd == "doctor":
            return api.doctor()

        if args.cmd == "check":
            return api.check(args.data_id)

        if args.cmd == "whoami":
            payload = api.whoami()
            data = payload.get("data") or {}
            if not data:
                dump(payload)
                return 1
            print(f"姓名     : {data.get('Name')}")
            print(f"学号     : {data.get('StudentId')}")
            print(f"学院     : {data.get('FacultyNmae')}")
            print(f"专业     : {data.get('CultureProfessionNmae')}")
            print(f"培养层次 : {data.get('TrainingLevelName')}")
            print(f"在可报名名单内 : {data.get('IsInStudentList')}")
            print(f"已完成人像采集 : {data.get('IsFinishPhoto')}")
            return 0

        if args.cmd == "list":
            payload = api.list_lectures(
                page=args.page,
                page_size=args.page_size,
                status=args.status,
                time_filter=args.time,
                class_level=args.level,
            )
            if args.json:
                dump(payload)
            else:
                print_list(payload)
            return 0

        if args.cmd == "detail":
            dump(api.detail(args.data_id))
            return 0

        if args.cmd == "signup":
            at = parse_time(args.at) if args.at else None
            if args.at and not at:
                print(f"× 无法解析 --at：{args.at}", file=sys.stderr)
                return 2
            return api.signup(
                args.data_id,
                at=at,
                lead=args.lead,
                patience=args.patience,
                interval=args.interval,
                once=args.now,
                dry_run=args.dry_run,
            )

        if args.cmd == "cancel":
            payload = api.cancel(args.data_id)
            if payload.get("state") == "success":
                print("√ 已取消报名")
                return 0
            dump(payload)
            return 1

    except TokenExpired as exc:
        print(f"× {exc}", file=sys.stderr)
        print("  请重新登录并更新登录凭证（见 README）。", file=sys.stderr)
        return 3
    except ApiError as exc:
        print(f"× {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
