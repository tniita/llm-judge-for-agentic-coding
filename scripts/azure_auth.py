"""Azure の資格情報の選び方をそろえる。

LLM_JUDGE_AUTH で切り替える:
  cli（既定）        az login 済みのユーザー、または CI の azure/login（OIDC）
  default            DefaultAzureCredential（Hosted Agent のマネージド ID を含む）
  managed_identity   ManagedIdentityCredential（AZURE_CLIENT_ID があればユーザー割り当て）

既定を cli にしているのは、DefaultAzureCredential だと VS Code など別テナントの資格情報を拾い、
Foundry のトークンのテナントが合わなくなることがあるため。
"""

import os


def credential():
    mode = (os.environ.get("LLM_JUDGE_AUTH") or "cli").lower()
    if mode == "cli":
        from azure.identity import AzureCliCredential
        return AzureCliCredential()
    if mode == "default":
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()
    if mode == "managed_identity":
        from azure.identity import ManagedIdentityCredential
        client_id = os.environ.get("AZURE_CLIENT_ID")
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    raise SystemExit(f"LLM_JUDGE_AUTH の値が不正です: {mode!r}（cli / default / managed_identity）")
