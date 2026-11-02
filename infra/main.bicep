// LLM Judge の可視化基盤（データ基盤・モデル基盤・オブザーバビリティ）
//
//   Log Analytics + Application Insights: 評価結果のカスタムイベント、APIM のトークン計測、Workbook
//   Cosmos DB（serverless）: 評価データの保存（scripts/publish_results.py が書き込む）
//   API Management: コーディングエージェント → Foundry の呼び出しを中継し、方式ごとのトークン利用量を計測する
//
// デプロイ方法は README の「可視化基盤」を参照。

@description('リソースの場所')
param location string = resourceGroup().location

@description('リソース名の接頭辞')
@minLength(3)
@maxLength(12)
param namePrefix string = 'llmjudge'

@description('評価対象のエージェントが呼び出す Foundry（AI Services）のエンドポイント。例: https://<account>.services.ai.azure.com')
param foundryEndpoint string = ''

@description('API Management の発行者メールアドレス（deployApim=true のとき必須）')
param publisherEmail string = ''

@description('API Management を作るか（トークン計測が不要なら false）')
param deployApim bool = true

@description('API Management の SKU。Anthropic Messages API のトークン計測（llm-emit-token-metric）は v2 レベルのみ対応')
@allowed([
  'BasicV2'
  'StandardV2'
])
param apimSku string = 'BasicV2'

@description('Cosmos DB に書き込めるようにするプリンシパル（ユーザーや CI のアプリ）の objectId')
param dataContributorPrincipalIds array = []

var suffix = uniqueString(resourceGroup().id, namePrefix)
var metricNamespace = 'llm-judge'
var dimensionsXml = loadTextContent('policies/dimensions.xml')

// ---------- オブザーバビリティ ----------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-log-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appi-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, namePrefix, 'llm-judge-workbook')
  location: location
  kind: 'shared'
  properties: {
    displayName: 'LLM Judge: 評価結果とトークン利用量'
    category: 'workbook'
    sourceId: appInsights.id
    serializedData: loadTextContent('workbook.json')
  }
}

// ---------- データ基盤 ----------

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: '${namePrefix}-cosmos-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [ { name: 'EnableServerless' } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [ { locationName: location, failoverPriority: 0 } ]
    // キーは使わず Entra ID で書き込む
    disableLocalAuth: true
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'llmjudge'
  properties: { resource: { id: 'llmjudge' } }
}

// type（attempt / run / variant / comparison / pairwise / pair）で種類を区別し、プロジェクト単位で分割する
resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDb
  name: 'results'
  properties: {
    resource: {
      id: 'results'
      partitionKey: { paths: [ '/project' ], kind: 'Hash' }
    }
  }
}

// 評価サービス（hosted/、Foundry Hosted Agent）の状態（実験・Run・タスクの比較・人間レビュー・ジョブ）。実験単位で分割する
resource cosmosServiceContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDb
  name: 'service'
  properties: {
    resource: {
      id: 'service'
      partitionKey: { paths: [ '/experiment_id' ], kind: 'Hash' }
    }
  }
}

// Cosmos DB Built-in Data Contributor
// 評価サービス（hosted/）を使う場合は、Hosted Agent のマネージド ID の objectId も dataContributorPrincipalIds に加える
resource cosmosWriters 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for principalId in dataContributorPrincipalIds: {
  parent: cosmos
  name: guid(cosmos.id, principalId, 'data-contributor')
  properties: {
    principalId: principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}]

// ---------- モデル基盤（API Management） ----------

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = if (deployApim) {
  name: '${namePrefix}-apim-${suffix}'
  location: location
  sku: { name: apimSku, capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: 'LLM Judge'
  }
}

resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = if (deployApim) {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsights.id
    credentials: { connectionString: appInsights.properties.ConnectionString }
  }
}

// metrics: true にしないと llm-emit-token-metric が Application Insights に届かない
resource apimDiagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = if (deployApim) {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: apimLogger.id
    metrics: true
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
  }
}

// OpenAI 互換 API（/openai/...）
resource openaiApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (deployApim) {
  parent: apim
  name: 'foundry-openai'
  properties: {
    displayName: 'Foundry (OpenAI compatible)'
    path: 'openai'
    protocols: [ 'https' ]
    serviceUrl: '${foundryEndpoint}/openai'
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'api-key', query: 'api-key' }
  }
}

// Anthropic Messages API（/anthropic/...）。Claude 系エージェント用
resource anthropicApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (deployApim) {
  parent: apim
  name: 'foundry-anthropic'
  properties: {
    displayName: 'Foundry (Anthropic)'
    path: 'anthropic'
    protocols: [ 'https' ]
    serviceUrl: '${foundryEndpoint}/anthropic'
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'x-api-key', query: 'api-key' }
  }
}

var methods = [ 'GET', 'POST' ]

resource openaiOps 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for m in methods: if (deployApim) {
  parent: openaiApi
  name: toLower('${m}-all')
  properties: { displayName: '${m} /*', method: m, urlTemplate: '/*' }
}]

resource anthropicOps 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for m in methods: if (deployApim) {
  parent: anthropicApi
  name: toLower('${m}-all')
  properties: { displayName: '${m} /*', method: m, urlTemplate: '/*' }
}]

// APIM メトリックは project / variant 単位。Run 単位の相関は publish_results.py のイベントで行う
resource openaiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (deployApim) {
  parent: openaiApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(loadTextContent('policies/openai.xml'), '__NAMESPACE__', metricNamespace), '__DIMENSIONS__', dimensionsXml)
  }
}

// OpenAI 互換と同じ llm-emit-token-metric で計測する（応答本文を読まないのでストリーミングを妨げない）
resource anthropicPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (deployApim) {
  parent: anthropicApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(loadTextContent('policies/anthropic.xml'), '__NAMESPACE__', metricNamespace), '__DIMENSIONS__', dimensionsXml)
  }
}

output appInsightsName string = appInsights.name
output logAnalyticsWorkspaceId string = logAnalytics.properties.customerId
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output apimGatewayUrl string = deployApim ? apim!.properties.gatewayUrl : ''
output apimPrincipalId string = deployApim ? apim!.identity.principalId : ''
output workbookId string = workbook.id
