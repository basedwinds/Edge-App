if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    $env:Path += ";C:\Program Files\nodejs"
}
Set-Location "$PSScriptRoot\..\frontend"
npm run dev
