[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CandidateRoot,
    [string]$CertificateThumbprint = '',
    [string]$TimestampServer = ''
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

try {
    $root = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
    $manifest = Get-Content -LiteralPath (Join-Path $root 'CLIENT_BUNDLE_MANIFEST.json') -Raw | ConvertFrom-Json
    if ([string]$manifest.sourceCommit -cnotmatch '^[0-9a-f]{40}$' -or
        [string]$manifest.buildStatus -cnotin @('local-candidate-not-published-release','ci-candidate-not-published-release')) { throw 'Expected a committed unpublished candidate stage.' }
    foreach ($part in @((Get-Item -LiteralPath $root -Force)) + @(Get-ChildItem -LiteralPath $root -Force -Recurse)) {
        if (($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Candidate contains a link or junction.' }
    }
    # This tool accepts an uninitialized packaging stage only. Never sign or
    # inventory an installed account, runtime, private state or database.
    foreach ($private in @('.env','.client-state','.venv','.runtime','.lan-server','LAN_CLIENT_KIT','database\attendance.db')) {
        if (Test-Path -LiteralPath (Join-Path $root $private)) { throw 'Expected a fresh, uninitialized candidate stage.' }
    }
    $files = @(Get-ChildItem -LiteralPath $root -File -Recurse | Where-Object { $_.Extension -in @('.ps1','.exe') } | Sort-Object FullName)
    if (($CertificateThumbprint -ne '') -xor ($TimestampServer -ne '')) { throw 'Select both a publisher certificate and timestamp server.' }
    if ($CertificateThumbprint -ne '') {
        if ([string]$manifest.buildStatus -cne 'local-candidate-not-published-release') { throw 'CI inspection cannot sign a candidate.' }
        if ($CertificateThumbprint -cnotmatch '^[0-9A-Fa-f]{40}$') { throw 'Invalid certificate thumbprint.' }
        $uri = $null
        if (-not [Uri]::TryCreate($TimestampServer,[UriKind]::Absolute,[ref]$uri) -or
            $uri.Scheme -cne 'http' -or $uri.UserInfo -ne '' -or $uri.Fragment -ne '') {
            throw 'Select the publisher timestamp service supported by Windows Authenticode.'
        }
        # The operator must supply an existing legitimate signing identity. This
        # code never creates a certificate, imports a key, changes trust or buys one.
        $certificate = Get-Item -LiteralPath ('Cert:\CurrentUser\My\' + $CertificateThumbprint)
        $codeSigning = @(
            foreach ($extension in $certificate.Extensions) {
                if ($extension.Oid.Value -ceq '2.5.29.37') {
                    $extension.EnhancedKeyUsages | Where-Object { $_.Value -ceq '1.3.6.1.5.5.7.3.3' }
                }
            }
        )
        if (-not $certificate.HasPrivateKey -or $codeSigning.Count -ne 1 -or
            $certificate.NotBefore -gt [DateTime]::Now -or $certificate.NotAfter -le [DateTime]::Now) {
            throw 'The selected certificate is not a current code-signing identity with its private key.'
        }
        foreach ($file in @($files | Where-Object { $_.Extension -eq '.ps1' })) {
            # Preserve vendor EXE bytes/identity and their pinned runtime hashes.
            $signed = Set-AuthenticodeSignature -LiteralPath $file.FullName -Certificate $certificate `
                -HashAlgorithm SHA256 -TimestampServer $TimestampServer -IncludeChain NotRoot
            if ([string]$signed.Status -cne 'Valid' -or $null -eq $signed.TimeStamperCertificate) {
                throw 'A script signature or trusted timestamp did not validate. Discard this unpublished stage.'
            }
        }
    }
    $records = @(
        foreach ($file in $files) {
            $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
            [pscustomobject]@{
                path = $file.FullName.Substring($root.Length + 1).Replace('\','/')
                sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                status = [string]$signature.Status
                signerThumbprint = if ($null -ne $signature.SignerCertificate) { $signature.SignerCertificate.Thumbprint } else { $null }
                timestamped = ($null -ne $signature.TimeStamperCertificate)
            }
        }
    )
    [pscustomobject]@{
        schemaVersion = 1
        qualification = 'publisher-and-target-policy-unqualified'
        firstPartyScriptsSigned = ($CertificateThumbprint -ne '')
        files = $records
    } | ConvertTo-Json -Depth 5 -Compress
}
catch {
    [Console]::Error.WriteLine('Publisher inspection/signing did not finish. No release is qualified; preserve the unpublished stage for review.')
    exit 1
}
