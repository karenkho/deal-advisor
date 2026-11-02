// Resource-group scoped hosting resources: Log Analytics, ACR, Container Apps environment + app, budget.
param location string
param appName string
param alertEmail string
param budgetAmount int
param budgetStartDate string
param aoaiEndpoint string
param aoaiApiVersion string
param aoaiMainDeployment string
param aoaiLightDeployment string
param aoaiEmbedDeployment string
@secure()
param aoaiApiKey string
@secure()
param appPassword string
param placeholderImage string
param maxRunUsd string
param maxDailyUsd string

var suffix = uniqueString(resourceGroup().id)
var acrName = 'tor2backlog${suffix}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Basic SKU, admin user enabled: the app pulls with admin credentials stored as a Container Apps secret
// (role assignments would need Owner rights). Capstone: managed identity + AcrPull instead.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8501
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        { name: 'aoai-api-key', value: aoaiApiKey }
        { name: 'app-password', value: appPassword }
        { name: 'acr-password', value: acr.listCredentials().passwords[0].value }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: placeholderImage
          resources: { cpu: json('0.5'), memory: '1.0Gi' }
          env: [
            { name: 'AZURE_OPENAI_ENDPOINT', value: aoaiEndpoint }
            { name: 'AZURE_OPENAI_API_KEY', secretRef: 'aoai-api-key' }
            { name: 'AZURE_OPENAI_API_VERSION', value: aoaiApiVersion }
            { name: 'AZURE_OPENAI_MAIN_DEPLOYMENT', value: aoaiMainDeployment }
            { name: 'AZURE_OPENAI_LIGHT_DEPLOYMENT', value: aoaiLightDeployment }
            { name: 'AZURE_OPENAI_EMBED_DEPLOYMENT', value: aoaiEmbedDeployment }
            { name: 'APP_PASSWORD', secretRef: 'app-password' }
            { name: 'MAX_TOR_PAGES', value: '60' }
            { name: 'MAX_FEATURES', value: '80' }
            { name: 'MAX_RUN_USD', value: maxRunUsd }
            { name: 'MAX_DAILY_USD', value: maxDailyUsd }
          ]
        }
      ]
      scale: {
        minReplicas: 0   // no charges while idle; set to 1 only during the demo window
        maxReplicas: 1
      }
    }
  }
}

module budget 'budget.bicep' = {
  name: '${appName}-budget'
  params: {
    budgetName: '${appName}-budget'
    budgetAmount: budgetAmount
    budgetStartDate: budgetStartDate
    alertEmail: alertEmail
  }
}

output appFqdn string = app.properties.configuration.ingress.fqdn
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
