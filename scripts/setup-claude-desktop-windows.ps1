<#
.SYNOPSIS
    Configures Claude Desktop on Windows to connect to the Ziese Memory MCP server.

.DESCRIPTION
    This script adds the ziese-memory MCP server configuration to Claude Desktop
    using the mcp-remote shim (requires Node.js/npx).

.NOTES
    Prerequisites:
    - Claude Desktop installed
    - Node.js installed (for npx)
    - Internet access to ziese-memory.midcityit.com

.EXAMPLE
    .\setup-claude-desktop-windows.ps1
#>

$ErrorActionPreference = "Stop"

# Configuration
$McpUrl = "https://ziese-memory.midcityit.com/mcp"
$McpToken = "e12cf0270e72a40bb85604ec259191663aad56f1659910b0eef1df1b5ff07569"
$ServerName = "ziese-memory"

# Claude Desktop config path
$ConfigPath = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
$ConfigDir = Split-Path $ConfigPath -Parent

Write-Host "=== Ziese Memory MCP Setup for Claude Desktop ===" -ForegroundColor Cyan
Write-Host ""

# Check Node.js
try {
    $nodeVersion = & node --version 2>$null
    Write-Host "[OK] Node.js found: $nodeVersion" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Node.js not found. Install from https://nodejs.org" -ForegroundColor Red
    exit 1
}

# Check npx
try {
    $npxVersion = & npx --version 2>$null
    Write-Host "[OK] npx found: $npxVersion" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] npx not found. Reinstall Node.js" -ForegroundColor Red
    exit 1
}

# Create config directory if needed
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Host "[OK] Created config directory: $ConfigDir" -ForegroundColor Green
}

# Load or create config
if (Test-Path $ConfigPath) {
    $config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
    Write-Host "[OK] Loaded existing config: $ConfigPath" -ForegroundColor Green
} else {
    $config = [PSCustomObject]@{}
    Write-Host "[OK] Creating new config" -ForegroundColor Green
}

# Ensure mcpServers key exists
if (-not $config.PSObject.Properties["mcpServers"]) {
    $config | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([PSCustomObject]@{})
}

# Add ziese-memory server
$serverConfig = [PSCustomObject]@{
    command = "npx"
    args = @(
        "-y",
        "mcp-remote",
        $McpUrl,
        "--header",
        "Authorization: Bearer $McpToken"
    )
}

# Check if already configured
if ($config.mcpServers.PSObject.Properties[$ServerName]) {
    Write-Host "[WARN] $ServerName already configured. Overwriting." -ForegroundColor Yellow
    $config.mcpServers.$ServerName = $serverConfig
} else {
    $config.mcpServers | Add-Member -NotePropertyName $ServerName -NotePropertyValue $serverConfig
}

# Write config
$config | ConvertTo-Json -Depth 10 | Set-Content $ConfigPath -Encoding UTF8
Write-Host ""
Write-Host "[OK] Configuration written to: $ConfigPath" -ForegroundColor Green

# Verify connectivity
Write-Host ""
Write-Host "Testing connectivity to $McpUrl..." -ForegroundColor Cyan
try {
    $response = Invoke-WebRequest -Uri "https://ziese-memory.midcityit.com/health" -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -eq 200) {
        Write-Host "[OK] Memory server is reachable" -ForegroundColor Green
    }
} catch {
    Write-Host "[WARN] Could not reach memory server. Check network/VPN." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Restart Claude Desktop" -ForegroundColor White
Write-Host "  2. Look for 'ziese-memory' tools in the MCP panel" -ForegroundColor White
Write-Host "  3. Test: ask Claude to 'search memories for M365'" -ForegroundColor White
Write-Host ""
Write-Host "Available tools after restart:" -ForegroundColor White
Write-Host "  - save_memory    (create/update memories)" -ForegroundColor Gray
Write-Host "  - search_memories (semantic search)" -ForegroundColor Gray
Write-Host "  - list_memories   (filtered list)" -ForegroundColor Gray
Write-Host "  - delete_memory   (remove by ID)" -ForegroundColor Gray
