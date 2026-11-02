// Resource-group budget with 50% / 90% actual-cost alerts. Used by hosting.bicep and deployable on its own
// (az deployment group create -g <resource-group> -f infra/budget.bicep ...) to change only the budget.
param budgetName string
param budgetAmount int
param budgetStartDate string
param alertEmail string

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [ alertEmail ]
      }
      actual90: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 90
        thresholdType: 'Actual'
        contactEmails: [ alertEmail ]
      }
    }
  }
}
