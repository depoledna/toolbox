from .generate_image import generate_image
from .edit_image import edit_image
from .list_packages import list_packages
from .parse_nmap import parse_nmap
from .categorize_hosts import categorize_hosts
from .pentest_report import pentest_report
from .xcode import testflight
from .apple_ads import apple_ads_keywords
from .generate_icon import generate_icon
from .connect_new_app import connect_new_app
from .asc_api import asc_api
from .connect_set_privacy import connect_set_privacy


def blob_list(env_path: str, prefix: str = "", limit: int = 100) -> str:
    from .vercel_blob import blob_list as _blob_list
    return _blob_list(env_path, prefix=prefix, limit=limit)


def blob_put(env_path: str, pathname: str, content: str = "", file_path: str = "") -> str:
    from .vercel_blob import blob_put as _blob_put
    return _blob_put(env_path, pathname=pathname, content=content, file_path=file_path)


def blob_get(env_path: str, url: str) -> str:
    from .vercel_blob import blob_get as _blob_get
    return _blob_get(env_path, url=url)


def blob_delete(env_path: str, urls: str) -> str:
    from .vercel_blob import blob_delete as _blob_delete
    return _blob_delete(env_path, urls=urls)


def blob_head(env_path: str, url: str) -> str:
    from .vercel_blob import blob_head as _blob_head
    return _blob_head(env_path, url=url)


def man() -> str:
    """Quick reference for all library functions."""
    return """library — image generation, editing, package management, blob, pentest utilities

library.generate_image(prompt, filename=None, path=None, rate_limit=3.0)
  generate_image("A cat")                              → ./a_cat.png
  generate_image("A cat", path="/tmp")                 → /tmp/a_cat.png
  generate_image(["A cat", "A dog"], path="./out")     → ["./out/a_cat.png", ...]

library.edit_image(image, prompt, filename=None, path=None, rate_limit=3.0)
  edit_image("photo.png", "Make B&W")                          → ./photo_edited.png
  edit_image(["a.png", "b.png"], "Add vignette", path="./out") → ["./out/a_edited.png", ...]

library.generate_icon(concept, style="modern", background="", filename=None, path=None)
  generate_icon("calendar with moon phases")                     → ./calendar_with_moon_phases.png
  generate_icon("music notes", style="3d-clay")                  → 3D clay style
  generate_icon("running shoe", background="deep blue gradient") → custom background
  Styles: modern, 3d-clay, flat, gradient, minimal, playful

library.list_packages(filter="")
  list_packages()           → all packages
  list_packages("numpy")    → filtered by name

library.blob_list(env_path, prefix="", limit=100)
library.blob_put(env_path, pathname, content="", file_path="")
library.blob_get(env_path, url)
library.blob_delete(env_path, urls)
library.blob_head(env_path, url)

library.testflight(project, scheme, api_key_id=None, issuer_id=None, api_key_path=None)
  testflight("Arcana Calendar.xcodeproj", "Arcana Calendar")

library.parse_nmap(raw)
  data = parse_nmap(nmap_output)      → {"hosts": [...], "summary": {...}}
  data["hosts"][0]["ip"]              → "192.168.1.1"
  data["hosts"][0]["ports"]           → [{"port": 80, "state": "open", ...}]

library.categorize_hosts(hosts)
  cats = categorize_hosts(data["hosts"])  → {"infrastructure": [...], "iot": [...], ...}
  for cat, devs in cats.items():
      print(f"{cat}: {len(devs)} devices")

library.pentest_report(findings, title="Assessment", output=None)
  findings = [{"target": "192.168.1.1", "severity": "CRITICAL", "category": "Creds",
               "title": "SSH admin/admin", "detail": "...", "remediation": "Change pw"}]
  report = pentest_report(findings, title="Camera Assessment")
  pentest_report(findings, output="/tmp/report.md")    → also writes to file

library.apple_ads_keywords(keyword, country="United States", top_n=5)
  apple_ads_keywords("ai chat")                        → keywords + suggested CPT bid + top 5 competitors
  apple_ads_keywords("coin flip", top_n=10)            → analyze top 10 App Store competitors
  apple_ads_keywords("fitness", raw=True)              → raw dict with all data
  apple_ads_keywords(["ai chat", "fitness", "games"])   → batch: parallel Chrome sessions (up to 5)

library.connect_new_app(name, bundle_id="", sku="", platform="ios", project="")
  connect_new_app("My App", project="MyApp.xcodeproj")                 → reads bundle ID from project
  connect_new_app("My App")                                            → auto: dp.My-App, sku=my-app
  connect_new_app("My App", "dp.My-App", "my-app-001")                → explicit bundle ID + SKU
  Auto-registers bundle ID on developer.apple.com if not found
  Returns: {"app_id": "123", "name": "...", "bundle_id_registered": true, ...}

library.asc_api(method, path, body=None)
  asc_api("GET", "/v1/apps")                                          → list all apps
  asc_api("GET", "/v1/apps?filter[bundleId]=dp.My-App")               → find app by bundle ID
  asc_api("PATCH", "/v1/ageRatingDeclarations/ID", {"data": {...}})   → update age rating
  Auto-handles JWT auth (cached, auto-refreshed)

library.connect_set_privacy(app_id, collects_data=False)
  connect_set_privacy("<APP_ID>")                                      → "No data collected", publishes
  Browser automation (no API exists for App Privacy)"""


__all__ = [
    "generate_image",
    "edit_image",
    "list_packages",
    "blob_list",
    "blob_put",
    "blob_get",
    "blob_delete",
    "blob_head",
    "testflight",
    "parse_nmap",
    "categorize_hosts",
    "pentest_report",
    "apple_ads_keywords",
    "generate_icon",
    "connect_new_app",
    "asc_api",
    "connect_set_privacy",
    "man",
]
