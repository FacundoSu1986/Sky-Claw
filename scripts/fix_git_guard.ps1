<#
.SYNOPSIS
    Permanent fix for auto-created .git directory in Skyclaw_Main_Sync.
.DESCRIPTION
    This script:
    1. Removes any existing .git directory
    2. Removes Claude Code worktrees (the primary trigger for git init)
    3. Creates a read-only sentinel FILE named ".git" to block git init
       (git init fails if .git exists as a file, not a directory)
.NOTES
    Root cause: Claude Code (.claude/worktrees/) and Kilo Code (initGit API)
    both auto-initialize git repos. The worktree feature requires a .git repo.
    $ProjectRoot defaults to the parent of the directory containing this script.
    Pass -ProjectRoot explicitly to target a different path.
#>

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RemoveWorktree,
    [switch]$Uninstall
)

# Validate project root exists
if (-not (Test-Path -Path $ProjectRoot -PathType Container)) {
    throw "Project root does not exist or is not a directory: $ProjectRoot"
}
$ProjectRoot = (Resolve-Path -Path $ProjectRoot -ErrorAction Stop).Path

# Validate this is actually the Sky-Claw repo (refuse to operate on arbitrary dirs)
$RepositoryMarkerPath = Join-Path $ProjectRoot "pyproject.toml"
if (-not (Test-Path -Path $RepositoryMarkerPath -PathType Leaf)) {
    throw "Refusing to modify '$ProjectRoot': repository marker 'pyproject.toml' not found. Pass -ProjectRoot explicitly if the repo root differs."
}

$GitPath = Join-Path $ProjectRoot ".git"
$WorktreePath = Join-Path $ProjectRoot ".claude\worktrees"
$SentinelContent = "# This file blocks git init. Do NOT delete or convert to directory.`n# Created by fix_git_guard.ps1`n# Root cause: Claude Code worktrees + Kilo Code initGit auto-create .git dirs`n"

function Remove-GitDirectory {
    # Safety: ensure ProjectRoot is set and non-empty before operating
    if (-not $ProjectRoot -or -not (Test-Path -Path $ProjectRoot -PathType Container)) {
        throw "Cannot remove .git: ProjectRoot '$ProjectRoot' is not a valid directory."
    }
    # Safety: ensure we only remove .git directly inside the validated project root
    $ExpectedGitPath = Join-Path (Resolve-Path $ProjectRoot).Path ".git"
    if ($GitPath -ne $ExpectedGitPath) {
        throw "GitPath '$GitPath' does not match expected path '$ExpectedGitPath'. Aborting to prevent accidental data loss."
    }
    if (Test-Path $GitPath -PathType Container) {
        Write-Host "[FIX] Removing existing .git directory..." -ForegroundColor Yellow
        Remove-Item -Path $GitPath -Recurse -Force
        Write-Host "[OK] .git directory removed." -ForegroundColor Green
    } else {
        Write-Host "[INFO] No .git directory found (already clean)." -ForegroundColor Cyan
    }
}

function Remove-ClaudeWorktrees {
    if (Test-Path $WorktreePath) {
        Write-Host "[FIX] Removing Claude Code worktrees (primary git-init trigger)..." -ForegroundColor Yellow
        Remove-Item -Path $WorktreePath -Recurse -Force
        Write-Host "[OK] Claude Code worktrees removed." -ForegroundColor Green
    } else {
        Write-Host "[INFO] No Claude Code worktrees found." -ForegroundColor Cyan
    }
}

function Install-GitSentinel {
    # Remove existing .git (file or directory)
    if (Test-Path $GitPath) {
        Remove-Item -Path $GitPath -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Create sentinel FILE (not directory) - git init will refuse to proceed
    Set-Content -Path $GitPath -Value $SentinelContent -NoNewline -Force
    Set-ItemProperty -Path $GitPath -Name IsReadOnly -Value $true

    Write-Host "[OK] Sentinel file '.git' created (read-only)." -ForegroundColor Green
    Write-Host "     git init will now FAIL with: 'fatal: E:\Skyclaw_Main_Sync\.git already exists'" -ForegroundColor DarkGray
}

function Uninstall-GitGuard {
    if (Test-Path $GitPath -PathType Leaf) {
        Set-ItemProperty -Path $GitPath -Name IsReadOnly -Value $false
        Remove-Item -Path $GitPath -Force
        Write-Host "[OK] Sentinel .git file removed." -ForegroundColor Green
    } elseif (Test-Path $GitPath -PathType Container) {
        Remove-Item -Path $GitPath -Recurse -Force
        Write-Host "[OK] .git directory removed." -ForegroundColor Green
    } else {
        Write-Host "[INFO] No .git found to remove." -ForegroundColor Cyan
    }
}

# ---- Main ----
Write-Host ""
Write-Host "=== Skyclaw .git Guard ===" -ForegroundColor Magenta
Write-Host ""

if ($Uninstall) {
    Uninstall-GitGuard
    Write-Host ""
    Write-Host "[DONE] Guard uninstalled. Extensions may recreate .git." -ForegroundColor Yellow
    exit 0
}

# Step 1: Remove existing .git
Remove-GitDirectory

# Step 2: Remove Claude worktrees if requested
if ($RemoveWorktree) {
    Remove-ClaudeWorktrees
} else {
    Write-Host ""
    Write-Host "[WARNING] Claude Code worktrees NOT removed (use -RemoveWorktree flag)." -ForegroundColor Yellow
    Write-Host "  The worktree at .claude/worktrees/ is the PRIMARY trigger for git init." -ForegroundColor Yellow
    Write-Host "  Run: .\fix_git_guard.ps1 -RemoveWorktree" -ForegroundColor Yellow
    Write-Host ""
}

# Step 3: Install sentinel
Install-GitSentinel

Write-Host ""
Write-Host "[DONE] .git guard installed." -ForegroundColor Green
Write-Host ""
Write-Host "Additional steps to prevent recurrence:" -ForegroundColor Cyan
Write-Host "  1. In Kilo Code settings, disable auto git init:" -ForegroundColor White
Write-Host "     Settings > Kilo Code > git.autoInit = false" -ForegroundColor DarkGray
Write-Host "  2. In Claude Code, avoid using the worktree/parallel feature" -ForegroundColor White
Write-Host "  3. To undo: .\fix_git_guard.ps1 -Uninstall" -ForegroundColor White
Write-Host ""
