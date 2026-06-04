// =============================================================================
// App shell - core infrastructure (Python Function App on Flex Consumption)
// Deployed at resource-group scope. The resource group is created by the
// infra GitHub workflow before this template runs.
//
// This is a clean-slate shell: Storage + observability + a Flex Consumption
// Function App (CORS-enabled) + a Free Static Web App. Add project-specific
// resources (data containers, queues, app settings) as you build.
// =============================================================================

targetScope = 'resourceGroup'

@description('Base name used to derive all resource names (lowercase alphanumeric, keep it short). CHANGE per project — CI overrides this via the AZURE_BASE_NAME Actions variable; this default is only the local/manual-deploy fallback.')
param baseName string = 'myapp'

@description('Short environment name, e.g. dev / prod.')
param environmentName string = 'dev'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Python version for the Function App runtime.')
param pythonVersion string = '3.13'

@description('Claude API key (passed from a GitHub secret). Optional — leave empty to deploy the bare shell; set it once you add a Claude agent.')
@secure()
param claudeApiKey string = ''

@description('IANA/Windows time zone for the timer triggers (ingestion window + daily build are Bratislava-local).')
param websiteTimeZone string = 'Central Europe Standard Time'

// ---- Scraper tuning (realestate/scraper.py reads these as env vars) ---------
// Defaults are timeout-safe for host.json's 5-min functionTimeout. To honor the
// charter 20-40s inter-page delay, raise functionTimeout AND these together.
@description('Min seconds between page fetches within one sweep.')
param scrapeMinDelayS string = '4'
@description('Max seconds between page fetches within one sweep.')
param scrapeMaxDelayS string = '9'
@description('Hard per-sweep page cap (pages x page size results; the ~990 guard).')
param scrapeMaxPages string = '33'
@description('Expected listings per full results page (used to detect the last page).')
param scrapePageSize string = '30'

// ---- Derived names ----------------------------------------------------------
var uniqueSuffix = uniqueString(resourceGroup().id)
var storageAccountName = take(toLower('st${baseName}${environmentName}${uniqueSuffix}'), 24)
var functionAppName = 'func-${baseName}-${environmentName}-${uniqueSuffix}'
var hostingPlanName = 'plan-${baseName}-${environmentName}'
var appInsightsName = 'appi-${baseName}-${environmentName}'
var logAnalyticsName = 'log-${baseName}-${environmentName}'
var staticSiteName = 'stapp-${baseName}-${environmentName}-${uniqueSuffix}'
var deploymentContainerName = 'deploymentpackage'
var deploymentStorageConnSettingName = 'DEPLOYMENT_STORAGE_CONNECTION_STRING'

// ---- Storage ----------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

// Medallion data lake container (bronze/silver/gold/meta/agent prefixes live here).
// Infra owns it so the app never relies on a runtime create_container().
resource realestateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'realestate'
  properties: {
    publicAccess: 'None'
  }
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'

// ---- Observability ----------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---- Flex Consumption hosting plan -----------------------------------------
resource hostingPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: hostingPlanName
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

// ---- Function App (Flex Consumption, Python) -------------------------------
resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: deploymentStorageConnSettingName
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
    siteConfig: {
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnectionString
        }
        {
          name: deploymentStorageConnSettingName
          value: storageConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'CLAUDE_API_KEY'
          value: claudeApiKey
        }
        // Timer triggers are scheduled in local time; the ingestion window
        // (06:00–22:00) and the daily build (~22:30) are Bratislava-local.
        {
          name: 'WEBSITE_TIME_ZONE'
          value: websiteTimeZone
        }
        // Scraper tuning — set on creation so no manual portal config is needed.
        {
          name: 'SCRAPE_MIN_DELAY_S'
          value: scrapeMinDelayS
        }
        {
          name: 'SCRAPE_MAX_DELAY_S'
          value: scrapeMaxDelayS
        }
        {
          name: 'SCRAPE_MAX_PAGES'
          value: scrapeMaxPages
        }
        {
          name: 'SCRAPE_PAGE_SIZE'
          value: scrapePageSize
        }
      ]
      // The Static Web App calls this API cross-origin (build-time
      // VITE_API_BASE = https://<func-host>/api). Allow its generated hostname;
      // Bicep resolves the dependency so the SWA is created first.
      cors: {
        allowedOrigins: [
          'https://${staticSite.properties.defaultHostname}'
        ]
      }
    }
  }
}

// ---- Static Web App (Free) --------------------------------------------------
// Public, no-auth SPA. Deployed via the SWA GitHub Action with a deployment
// token (provider: None = no SWA-managed repo integration). Free tier keeps
// this within a low cost budget.
// NOTE: Static Web Apps are only offered in a subset of regions
// (e.g. westeurope, eastus2, westus2, centralus, eastasia).
resource staticSite 'Microsoft.Web/staticSites@2024-04-01' = {
  name: staticSiteName
  location: location
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    provider: 'None'
  }
}

// ---- Outputs ----------------------------------------------------------------
output functionAppName string = functionApp.name
output functionAppDefaultHostname string = functionApp.properties.defaultHostName
output storageAccountName string = storageAccount.name
output resourceGroupName string = resourceGroup().name
output staticWebAppName string = staticSite.name
output staticWebAppHostname string = staticSite.properties.defaultHostname
