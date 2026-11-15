# Read local Windows tables only. No discovery, DNS lookup, connection test,
# camera access or network configuration. Called by a bounded background job.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$result = @{ routesAvailable=$false; neighborsAvailable=$false; routes=@(); neighbors=@(); neighborsTruncated=$false }
try {
    $result.routes = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore |
        Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } |
        Select-Object -First 32 |
        ForEach-Object { @{ interface=[string]$_.InterfaceAlias; gateway=[string]$_.NextHop } })
    $result.routesAvailable = $true
} catch { }
try {
    $rows = @(Get-NetNeighbor -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -ne '255.255.255.255' -and $_.IPAddress -notmatch '^(224|239)\.' } |
        Select-Object -First 129)
    $result.neighborsTruncated = $rows.Count -gt 128
    $result.neighbors = @($rows | Select-Object -First 128 | ForEach-Object {
        @{ interface=[string]$_.InterfaceAlias; address=[string]$_.IPAddress;
           mac=[string]$_.LinkLayerAddress; state=[string]$_.State }
    })
    $result.neighborsAvailable = $true
} catch { }
$result | ConvertTo-Json -Depth 4 -Compress
