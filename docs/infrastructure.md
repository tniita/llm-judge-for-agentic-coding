# 可視化基盤（Azure）

[README に戻る](../README.md)

[infra/main.bicep](../infra/main.bicep) で、データ基盤・モデル基盤・オブザーバビリティをまとめて作ります（このリポジトリではデプロイしていません）。

| リソース | 役割 |
|---|---|
| Log Analytics + Application Insights | 評価結果のカスタムイベント、APIM のトークン計測、ログ。[infra/queries.kql](../infra/queries.kql) にアドホック分析用のクエリ |
| Workbook（[infra/workbook.json](../infra/workbook.json)） | プロジェクト別の評価スコア比較、合格率・コスト、指標の差分、トークン利用量、人間レビュー対象の一覧 |
| Cosmos DB（serverless、ローカル認証無効） | 評価データの保存（`publish_results.py` が Entra ID で書き込む） |
| API Management | エージェント → Foundry の呼び出しを中継し、`llm-emit-token-metric`（OpenAI 互換・Anthropic Messages API とも。応答本文を読まないのでストリーミングを妨げない）で方式ごとのトークン利用量を計測。Anthropic の計測は v2 レベルのみ対応のため、SKU は BasicV2 / StandardV2 |

```bash
az group create -n rg-llm-judge -l japaneast
az deployment group create -g rg-llm-judge -f infra/main.bicep \
  -p foundryEndpoint=https://<account>.services.ai.azure.com publisherEmail=<mail> \
     dataContributorPrincipalIds="[\"$(az ad signed-in-user show --query id -o tsv)\"]"

# APIM のマネージド ID に Foundry を呼び出す権限を付ける
az role assignment create --assignee <出力の apimPrincipalId> --role "Cognitive Services User" --scope <Foundry リソースの ID>
# publish_results.py に渡す接続文字列
az monitor app-insights component show -g rg-llm-judge -a <出力の appInsightsName> --query connectionString -o tsv
```

- エージェントの実行基盤は APIM のゲートウェイ（`<apimGatewayUrl>/openai` または `/anthropic`）を呼び、`x-llm-judge-project` / `x-llm-judge-variant` ヘッダーを付けます。メトリックのディメンションは API ID・project・variant のみです。APIM の上限（ディメンションごとに 100 値、名前空間ごとに 1,000 時系列）を超えない固定のラベルを使い、Run ID や日時を混ぜないでください
- Run 単位のトークン・コストと評価結果の相関には `LlmJudgeAttempt` イベントを使います。実行基盤は `make_row.py --tokens-in ... --tokens-out ... --cost-usd ...`（または CI inputs）に集計値を渡してください。APIM メトリックから Run ごとの値は復元しません。未計測は Workbook で空欄となり、トークン対スコアの散布図からは除外されます
- トークン計測をディメンション付きで見るには、Application Insights の「使用量と推定コスト」で**カスタム メトリックのディメンションでのアラートを有効化**してください（Bicep では設定できません）
- CI の OIDC 用アプリにも Cosmos DB に書き込ませる場合は、そのサービス プリンシパルの objectId を `dataContributorPrincipalIds` に加えます
