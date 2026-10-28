def check_level(screenshot: bool, crawl: bool, use_proxy: bool, mobile_ua: bool) -> str:
    if crawl or use_proxy or mobile_ua:
        return "Full"
    if screenshot:
        return "Visual"
    return "Status"
