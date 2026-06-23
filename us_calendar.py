# -*- coding: utf-8 -*-
"""
NYSE 交易日历 — 自包含、零依赖（仅用标准库）。
用途：
  1) auto-skip：判断"现在"对标的美东日期是否为交易日，非交易日让简报任务直接 bail。
  2) date-stamp：给出应盖在标题/文件名上的"美股交易日"，自动处理夏令时，
     不再手算冬夏令时 +1 小时。

设计前提（与你的排期吻合）：
  - 盘前任务跑在北京 20:00/21:00 → 美东当天清晨（开盘前），美东日期 = 当天 session。
  - 盘后任务跑在北京 07:00/07:45 → 美东前一日傍晚（收盘后），美东日期 = 当天 session。
  两类任务转成美东时间后，"美东日历日"正好等于它们关心的那个交易 session，
  所以核心逻辑统一为一句话：取美东当前日期，问它是不是 NYSE 交易日。

  覆盖的是 2007 年至今的 DST 规则与 NYSE 假日规则，足够覆盖当前及未来日期。
"""
from datetime import datetime, date, timedelta, timezone

# ----- 一次性临时休市（国丧日等，不可预测，按需手动增补）-----
ONE_OFF_CLOSURES = {
    date(2025, 1, 9),   # 卡特国丧日
    # date(2018,12, 5), # 老布什国丧日（历史，留作格式示例）
}

# ---------------- 工具函数 ----------------
def _nth_weekday(year, month, weekday, n):
    """月内第 n 个 weekday（weekday: Mon=0..Sun=6；n 从 1 起）。"""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))

def _last_weekday(year, month, weekday):
    """月内最后一个 weekday。"""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)

def _easter(year):
    """复活节（公历，Meeus/Jones/Butcher 算法）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)

def _et_offset_hours(now_utc):
    """美东相对 UTC 的小时偏移：EDT=-4 / EST=-5。DST：3月第2个周日~11月第1个周日。"""
    y = now_utc.year
    start = _nth_weekday(y, 3, 6, 2)   # 3月第2个周日
    end = _nth_weekday(y, 11, 6, 1)    # 11月第1个周日
    start_utc = datetime(start.year, start.month, start.day, 7, tzinfo=timezone.utc)  # 02:00 EST=07:00 UTC
    end_utc = datetime(end.year, end.month, end.day, 6, tzinfo=timezone.utc)          # 02:00 EDT=06:00 UTC
    return -4 if start_utc <= now_utc < end_utc else -5

def _nyse_full_holidays(year):
    """NYSE 全天休市日集合（含周末顺延规则）。"""
    hols = set()

    def observed(d, allow_friday=True):
        # 周六->前一个周五（元旦不顺延：allow_friday=False）；周日->后一个周一
        if d.weekday() == 5:
            return d - timedelta(days=1) if allow_friday else None
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    ny = observed(date(year, 1, 1), allow_friday=False)  # 元旦落周六不补周五（NYSE 规则）
    if ny:
        hols.add(ny)
    hols.add(_nth_weekday(year, 1, 0, 3))    # MLK：1月第3个周一
    hols.add(_nth_weekday(year, 2, 0, 3))    # 总统日：2月第3个周一
    hols.add(_easter(year) - timedelta(days=2))  # 耶稣受难日
    hols.add(_last_weekday(year, 5, 0))      # 阵亡将士纪念日：5月最后周一
    if year >= 2022:                          # 六月节自 2022 起为市场假日
        jt = observed(date(year, 6, 19))
        if jt:
            hols.add(jt)
    idd = observed(date(year, 7, 4))         # 独立日
    if idd:
        hols.add(idd)
    hols.add(_nth_weekday(year, 9, 0, 1))    # 劳动节：9月第1个周一
    hols.add(_nth_weekday(year, 11, 3, 4))   # 感恩节：11月第4个周四
    xmas = observed(date(year, 12, 25))      # 圣诞
    if xmas:
        hols.add(xmas)
    return hols

def _early_closes(year):
    """半日市（13:00 ET 提前收盘）。仍是交易日，不影响 should_run，只做提示。
    可靠项：感恩节次日。July 3 / Dec 24 各年规则不一，此处按常见规则 best-effort。"""
    s = set()
    s.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # 感恩节次日（周五）
    # 平安夜：落在周一~周四时通常半日市
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 4:
        s.add(dec24)
    # 7月3日：当 7/4 为交易日且 7/3 为工作日时通常半日市
    jul3 = date(year, 7, 3)
    if date(year, 7, 4).weekday() < 5 and jul3.weekday() < 5:
        s.add(jul3)
    return s


# ---------------- 主入口 ----------------
def us_trading_session(now_utc=None, mode="auto"):
    """
    返回 dict：
      should_run    : bool      是否交易日（可运行）
      trading_date  : str|None  'YYYY-MM-DD'，盖在标题/文件名上的美股交易日
      weekday       : str       美东星期
      is_early_close: bool      是否半日市
      reason        : str       运行/跳过说明
      run_note      : str       建议放在标题下方的"北京生成时间"注脚
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    off = _et_offset_hours(now_utc)
    now_et = now_utc + timedelta(hours=off)
    et_date = now_et.date()
    bj = now_utc + timedelta(hours=8)
    # mode="post": 若美东时间在当日收盘前(ET<16:00)，退回前一交易日
    if mode == "post" and now_et.hour < 16:
        et_date -= timedelta(days=1)
        while et_date.weekday() >= 5 or et_date in (_nyse_full_holidays(et_date.year) | ONE_OFF_CLOSURES):
            et_date -= timedelta(days=1)
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][et_date.weekday()]
    tz = "EDT" if off == -4 else "EST"
    run_note = (f"北京生成时间 {bj:%Y-%m-%d %H:%M} CST"
                f"（美东 {now_et:%Y-%m-%d %H:%M} {tz}）")

    base = {"weekday": wd, "is_early_close": False, "run_note": run_note,
            "et_date": et_date.isoformat()}

    if et_date.weekday() >= 5:
        return {**base, "should_run": False, "trading_date": None,
                "reason": f"周末（美东 {et_date} {wd}），跳过"}

    hols = _nyse_full_holidays(et_date.year) | ONE_OFF_CLOSURES
    if et_date in hols:
        return {**base, "should_run": False, "trading_date": None,
                "reason": f"美股休市（{et_date} {wd}），跳过"}

    early = et_date in _early_closes(et_date.year)
    return {**base, "should_run": True, "trading_date": et_date.isoformat(),
            "is_early_close": early,
            "reason": f"正常交易日{'（半日市 13:00 ET 提前收盘）' if early else ''}"}
