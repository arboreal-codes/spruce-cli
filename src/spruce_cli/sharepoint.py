"""SharePoint access: delegated MSAL login, REST and CSOM calls.

The same operations PnP.PowerShell and CLI for Microsoft 365 use, called directly so the
tool runs anywhere uv runs (macOS, Linux, Windows).
"""

import json
import os
import time
from typing import Any
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

import msal
import requests

from spruce_cli.common import HOME_DIR, Config, UserError

TENANT_TYPE = "{268004ae-ef6b-4e9b-8425-127220d84719}"
FILTER_TYPE = "{b92aeee2-c92c-4b67-abcc-024e471bc140}"
CSOM_HEAD = (
    '<Request AddExpandoFieldTypeSuffix="true" SchemaVersion="15.0.0.0" LibraryVersion="16.0.0.0" '
    'ApplicationName="spruce" xmlns="http://schemas.microsoft.com/sharepoint/clientquery/2009">'
)


class SharePointError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def origin(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise UserError(f"Not an absolute URL: {url}")
    return f"{parts.scheme}://{parts.netloc}"


def server_relative(url: str) -> str:
    return url[len(origin(url)) :] or "/"


def odata_string(value: str) -> str:
    """Quote a value for an OData @alias parameter: '...' with doubled quotes, URL-encoded."""
    return quote("'" + value.replace("'", "''") + "'", safe="")


def parse_result(payload: Any) -> Any:
    """Service operations return a JSON string in 'value'; decode it when possible."""
    value = payload.get("value", payload) if isinstance(payload, dict) else payload
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {"raw": value}
    return value


class SharePoint:
    def __init__(self, config: Config) -> None:
        if not config.admin_url or not config.client_id:
            raise UserError("admin_url and client_id must be set in spruce.toml (see config.example.toml)")
        self.config = config
        self.cache = msal.SerializableTokenCache()
        if config.token_cache.exists():
            self.cache.deserialize(config.token_cache.read_text(encoding="utf-8"))
        self.app = msal.PublicClientApplication(
            config.client_id,
            authority=f"https://login.microsoftonline.com/{config.tenant}",
            token_cache=self.cache,
        )
        self.http = requests.Session()

    # ------------------------------------------------------------ auth

    def _save_cache(self) -> None:
        if not self.cache.has_state_changed:
            return
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        self.config.token_cache.write_text(self.cache.serialize(), encoding="utf-8")
        if os.name == "posix":
            self.config.token_cache.chmod(0o600)

    def token(self, url: str, interactive: bool = True) -> str:
        scopes = [f"{origin(url)}/.default"]
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
        if not result:
            if not interactive:
                raise UserError("Not logged in. Run: spruce login")
            if self.config.auth == "device-code":
                flow = self.app.initiate_device_flow(scopes=scopes)
                if "user_code" not in flow:
                    raise UserError(f"Device code login failed: {flow.get('error_description')}")
                print(flow["message"])
                result = self.app.acquire_token_by_device_flow(flow)
            else:
                result = self.app.acquire_token_interactive(scopes, prompt="select_account")
        if "access_token" not in result:
            raise UserError(f"Login failed: {result.get('error_description') or result.get('error')}")
        self._save_cache()
        return result["access_token"]

    def login(self) -> str:
        self.token(self.config.admin_url)
        return self.app.get_accounts()[0]["username"]

    def account(self) -> str | None:
        accounts = self.app.get_accounts()
        return accounts[0]["username"] if accounts else None

    @staticmethod
    def logout(config: Config) -> None:
        config.token_cache.unlink(missing_ok=True)

    # ------------------------------------------------------------ HTTP

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        all_headers = {"Accept": "application/json;odata=nometadata"}
        if json is not None:
            all_headers["Content-Type"] = "application/json;odata=verbose"
        all_headers.update(headers or {})
        response: requests.Response | None = None
        for attempt in range(6):
            all_headers["Authorization"] = f"Bearer {self.token(url, interactive=False)}"
            response = self.http.request(
                method,
                url,
                headers=all_headers,
                json=json,
                data=data,
                stream=stream,
                timeout=300,
            )
            if response.status_code not in (429, 503):
                break
            wait = int(response.headers.get("Retry-After") or 5 * 2**attempt)
            print(f"Throttled by SharePoint, waiting {wait}s...")
            time.sleep(wait)
        assert response is not None
        if not response.ok:
            raise SharePointError(
                f"{method} {url}: {response.status_code} {error_text(response)}",
                response.status_code,
            )
        return response

    def get(self, url: str) -> Any:
        return self.request("GET", url).json()

    def get_all(self, url: str) -> list[dict[str, Any]]:
        """GET a REST collection following odata.nextLink."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            page = self.get(next_url)
            items.extend(page.get("value", []))
            next_url = page.get("odata.nextLink")
        return items

    def post(self, url: str, body: Any = None) -> Any:
        response = self.request("POST", url, json=body)
        return response.json() if response.content else None

    def download(self, url: str, target: os.PathLike[str]) -> None:
        with (
            self.request("GET", url, stream=True) as response,
            open(target, "wb") as handle,
        ):
            handle.writelines(response.iter_content(chunk_size=1 << 20))

    # ------------------------------------------------------------ CSOM (tenant admin)

    def csom(self, actions: str, object_paths: str) -> list[Any]:
        xml = f"{CSOM_HEAD}<Actions>{actions}</Actions><ObjectPaths>{object_paths}</ObjectPaths></Request>"
        response = self.request(
            "POST",
            f"{self.config.admin_url}/_vti_bin/client.svc/ProcessQuery",
            data=xml,
            headers={"Content-Type": "text/xml", "Accept": "application/json"},
        ).json()
        error = response[0].get("ErrorInfo")
        if error:
            raise SharePointError(f"CSOM error: {error.get('ErrorMessage')}")
        return response

    def list_sites(self) -> list[dict[str, Any]]:
        """All site collections with storage figures (Tenant.GetSitePropertiesFromSharePointByFilters)."""
        sites: list[dict[str, Any]] = []
        start = ""
        while True:
            response = self.csom(
                '<ObjectPath Id="4" ObjectPathId="3" /><Query Id="5" ObjectPathId="3">'
                '<Query SelectAllProperties="true"><Properties>'
                '<Property Name="NextStartIndexFromSharePoint" ScalarProperty="true" /></Properties></Query>'
                '<ChildItemQuery SelectAllProperties="true"><Properties /></ChildItemQuery></Query>',
                f'<Method Id="3" ParentId="1" Name="GetSitePropertiesFromSharePointByFilters"><Parameters>'
                f'<Parameter TypeId="{FILTER_TYPE}">'
                '<Property Name="Filter" Type="String"></Property>'
                '<Property Name="IncludeDetail" Type="Boolean">false</Property>'
                '<Property Name="IncludePersonalSite" Type="Enum">0</Property>'
                f'<Property Name="StartIndex" Type="String">{escape(start)}</Property>'
                '<Property Name="Template" Type="String"></Property>'
                f'</Parameter></Parameters></Method><Constructor Id="1" TypeId="{TENANT_TYPE}" />',
            )
            page = next(x for x in response if isinstance(x, dict) and "_Child_Items_" in x)
            sites.extend(page["_Child_Items_"])
            start = page.get("NextStartIndexFromSharePoint") or ""
            if not start:
                return sites

    def site_properties(self, site_url: str) -> dict[str, Any]:
        response = self.csom(
            '<Query Id="4" ObjectPathId="3"><Query SelectAllProperties="true"><Properties /></Query></Query>',
            '<Method Id="3" ParentId="1" Name="GetSitePropertiesByUrl"><Parameters>'
            f'<Parameter Type="String">{escape(site_url)}</Parameter><Parameter Type="Boolean">true</Parameter>'
            f'</Parameters></Method><Constructor Id="1" TypeId="{TENANT_TYPE}" />',
        )
        return next(x for x in response if isinstance(x, dict) and "StorageUsage" in x)

    def set_site_properties(self, site_url: str, properties: list[tuple[str, str, object]]) -> None:
        """Set tenant SiteProperties (name, CSOM type, value) and Update, like Set-SPOSite."""
        actions = "".join(
            f'<SetProperty Id="{10 + i}" ObjectPathId="5" Name="{name}">'
            f'<Parameter Type="{kind}">{str(value).lower() if isinstance(value, bool) else value}</Parameter>'
            "</SetProperty>"
            for i, (name, kind, value) in enumerate(properties)
        )
        self.csom(
            actions + '<ObjectPath Id="90" ObjectPathId="8" /><ObjectIdentityQuery Id="91" ObjectPathId="5" />',
            '<Method Id="5" ParentId="3" Name="GetSitePropertiesByUrl"><Parameters>'
            f'<Parameter Type="String">{escape(site_url)}</Parameter><Parameter Type="Boolean">false</Parameter>'
            '</Parameters></Method><Method Id="8" ParentId="5" Name="Update" />'
            f'<Constructor Id="3" TypeId="{TENANT_TYPE}" />',
        )


def error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("odata.error") or payload.get("error") or {}
    message = error.get("message")
    if isinstance(message, dict):
        return str(message.get("value", ""))
    return str(message or payload)[:300]
