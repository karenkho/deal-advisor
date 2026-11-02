// TOR-to-Backlog Copilot POC — all hosting resources in ONE new resource group (subscription-scope deployment).
// The existing Azure OpenAI resource lives in another resource group/subscription and is never touched here.
targetScope = 'subscription'

@description('Region for the resource group and all resources.')
param location string = 'southeastasia'

@description('New resource group (must not exist).')
param rgName string

@description('Container App name.')
param appName string

@description('Email for budget alerts.')
param alertEmail string

@description('Budget amount in USD for the hosting resource group.')
param budgetAmount int = 10

@description('Budget start: first day of the current month (UTC).')
param budgetStartDate string = utcNow('yyyy-MM-01')

@description('Azure OpenAI endpoint (existing resource, read via env).')
param aoaiEndpoint string
param aoaiApiVersion string = '2024-10-21'
param aoaiMainDeployment string = 'gpt-4.1-mini'
param aoaiLightDeployment string = 'gpt-4.1-nano'
param aoaiEmbedDeployment string = 'text-embedding-3-small'

@secure()
@description('Azure OpenAI API key — passed at deploy time from .env, never stored in parameters files.')
param aoaiApiKey string

@secure()
@description('Shared UI password — passed at deploy time from .env.')
param appPassword string

@description('Public placeholder image; replaced by the ACR image after the push.')
param placeholderImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

param maxRunUsd string = '0.60'
param maxDailyUsd string = '2.00'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: location
  tags: {
    project: appName
    data: 'synthetic-only'
  }
}

module hosting 'hosting.bicep' = {
  name: '${appName}-hosting'
  scope: rg
  params: {
    location: location
    appName: appName
    alertEmail: alertEmail
    budgetAmount: budgetAmount
    budgetStartDate: budgetStartDate
    aoaiEndpoint: aoaiEndpoint
    aoaiApiVersion: aoaiApiVersion
    aoaiMainDeployment: aoaiMainDeployment
    aoaiLightDeployment: aoaiLightDeployment
    aoaiEmbedDeployment: aoaiEmbedDeployment
    aoaiApiKey: aoaiApiKey
    appPassword: appPassword
    placeholderImage: placeholderImage
    maxRunUsd: maxRunUsd
    maxDailyUsd: maxDailyUsd
  }
}

output resourceGroup string = rg.name
output appFqdn string = hosting.outputs.appFqdn
output acrLoginServer string = hosting.outputs.acrLoginServer
output acrName string = hosting.outputs.acrName
