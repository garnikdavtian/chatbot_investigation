import base64
import hashlib

from investigator import web


def test_csp_allows_exactly_the_pages_one_script():
    page = web.PAGE.read_bytes()
    assert page.count(b"<script") == 1 and b"<script>" in page  # a second script would be blocked
    script = page.split(b"<script>")[1].split(b"</script>")[0]
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    assert f"script-src 'sha256-{digest}';" in web.csp(page)
    assert b" onclick=" not in page  # inline handlers are blocked too; the page uses addEventListener
