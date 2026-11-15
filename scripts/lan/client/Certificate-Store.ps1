# All operations are limited to the current Windows user's trusted roots.
# Accept an already reviewed certificate object, never reread a mutable file
# after a confirmation dialog has approved different bytes.
function Get-CsdTrustedCertificate {
    param([Parameter(Mandatory = $true)][string]$Thumbprint)
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root','CurrentUser')
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
        foreach ($certificate in $store.Certificates) {
            if ($certificate.Thumbprint -ceq $Thumbprint) { Write-Output $certificate }
        }
    }
    finally { $store.Close() }
}

function Add-CsdTrustedCertificate {
    param([Parameter(Mandatory = $true)][Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)
    if ($Certificate.HasPrivateKey) { throw 'A connection kit cannot install a private key.' }
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root','CurrentUser')
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($Certificate)
    }
    finally { $store.Close() }
}

function Remove-CsdTrustedCertificate {
    param([Parameter(Mandatory = $true)][Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root','CurrentUser')
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $matches = @($store.Certificates | Where-Object { $_.Thumbprint -ceq $Certificate.Thumbprint })
        if ($matches.Count -ne 1 -or
            [Convert]::ToBase64String($matches[0].RawData) -cne [Convert]::ToBase64String($Certificate.RawData)) {
            throw 'The trusted certificate changed after it was reviewed. Nothing was removed.'
        }
        $store.Remove($Certificate)
    }
    finally { $store.Close() }
}
