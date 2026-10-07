from investigator import web


def test_page_has_no_inline_script_and_the_csp_allows_only_files_from_this_server():
    page = (web.STATIC / "index.html").read_bytes()
    assert page.count(b"<script") == 1 and b'<script src="/app.js"></script>' in page
    assert b" onclick=" not in page  # inline handlers are blocked too; the script uses addEventListener
    assert "script-src 'self';" in web.CSP and "unsafe-inline" not in web.CSP.split("script-src")[1].split(";")[0]


def test_nginx_sends_the_same_csp():
    conf = (web.STATIC.parent.parent / "docker" / "nginx.conf").read_text()
    assert f'add_header Content-Security-Policy "{web.CSP}" always;' in conf
