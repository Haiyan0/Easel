<#
.SYNOPSIS
    只读诊断完整 Desktop 开发拓扑（实际配置/端口/挂载/密钥/provisioner）。
#>
[CmdletBinding()]
param(
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$Distro = 'Ubuntu-26.04',
    [string]$DataRoot = 'C:\Easel\desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861,
    [int]$ProvisionerGid = 1000
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

$script:failures = @()
function Add-Failure {
    param([Parameter(Mandatory)][string]$Code)
    $script:failures += $Code
    Write-Output "error=$Code"
}

function Invoke-Wsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & wsl.exe @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        ExitCode = [int]$code
        Output = @($lines | ForEach-Object { "$_" })
    }
}

function Get-EaselEngineIdentity {
    $windows = Invoke-EaselDocker -Arguments @(
        'info', '--format', '{{.ID}}')
    $windowsId = (($windows.Output -join '').Trim())
    if ($windows.ExitCode -ne 0 -or
        $windowsId -notmatch '^[0-9A-Za-z:._-]{8,128}$') {
        throw 'docker_engine_unavailable'
    }
    $wsl = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'info', '--format', '{{.ID}}')
    $wslId = (($wsl.Output -join '').Trim())
    if ($wsl.ExitCode -ne 0 -or
        $wslId -notmatch '^[0-9A-Za-z:._-]{8,128}$') {
        throw 'wsl_docker_engine_unavailable'
    }
    if ($windowsId -ne $wslId) {
        throw "docker_engine_mismatch:$windowsId/$wslId"
    }
    $context = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'context', 'show')
    $contextName = (($context.Output -join '').Trim())
    if ($context.ExitCode -ne 0 -or -not $contextName) {
        throw 'docker_context_unavailable'
    }
    return [pscustomobject]@{ Context = $contextName; Id = $wslId }
}

function Get-EaselSocketIdentity {
    $uid = Invoke-Wsl -Arguments @('-d', $Distro, '--', 'id', '-u')
    $gid = Invoke-Wsl -Arguments @('-d', $Distro, '--', 'id', '-g')
    $uidText = (($uid.Output -join '').Trim())
    $gidText = (($gid.Output -join '').Trim())
    $uidValue = 0
    $gidValue = 0
    if ($uid.ExitCode -ne 0 -or
        -not [int]::TryParse($uidText, [ref]$uidValue) -or $uidValue -lt 0) {
        throw 'provisioner_socket_owner_unknown'
    }
    if ($gid.ExitCode -ne 0 -or
        -not [int]::TryParse($gidText, [ref]$gidValue) -or $gidValue -lt 0) {
        throw 'provisioner_socket_group_unknown'
    }
    if ($gidValue -ne $ProvisionerGid) {
        throw "provisioner_gid_mismatch:$gidValue/$ProvisionerGid"
    }
    return [pscustomobject]@{ Uid = $uidValue; Gid = $gidValue }
}

function Invoke-ProvisionerIdentityCheck {
    param(
        [Parameter(Mandatory)][string]$SocketPath,
        [Parameter(Mandatory)][string]$ExpectedFile,
        [Parameter(Mandatory)][string]$Image
    )
    $wslExpected = ConvertTo-EaselWslPath -Path $ExpectedFile
    return Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'run', '--rm',
        '--network', 'none', '--read-only',
        '--user', '10001:10001', '--group-add', [string]$ProvisionerGid,
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'python',
        '--mount', "type=bind,src=$SocketPath,dst=/run/provisioner.sock,readonly",
        '--mount', "type=bind,src=$wslExpected,dst=/expected.json,readonly",
        $Image,
        '/opt/easel/deploy/desktop/provisioner_identity.py',
        'check', '--socket', '/run/provisioner.sock',
        '--expected', '/expected.json')
}

try {
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $Port = Resolve-EaselPort -Port $Port
    Assert-EaselDocker
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    $publicDir = Join-Path $DataRoot 'public'
    $socket = "/tmp/easel-runtime/$ProjectName/provisioner.sock"

    $networkState = 'missing'
    $inspect = Invoke-EaselDocker -Arguments @('network', 'inspect', $network)
    if ($inspect.ExitCode -eq 0) {
        $networkState = 'present'
    }
    Write-Output "network=$network state=$networkState"
    if ($networkState -ne 'present') {
        Add-Failure 'network_missing'
    }
    else {
        try {
            Assert-EaselNetworkContract -ProjectName $ProjectName `
                -NetworkName $network
        }
        catch {
            Add-Failure "network_invalid:$($_.Exception.Message)"
        }
    }

    foreach ($spec in @(
            [pscustomobject]@{ Name = $volumes.Control; Role = 'control' },
            [pscustomobject]@{ Name = $volumes.Keys; Role = 'keys' },
            [pscustomobject]@{ Name = $volumes.Egress; Role = 'egress-policy' })) {
        $volume = Invoke-EaselDocker -Arguments @(
            'volume', 'inspect', '--format', '{{json .Labels}}', $spec.Name)
        if ($volume.ExitCode -ne 0) {
            Write-Output "volume=$($spec.Name) state=missing"
            Add-Failure "volume_missing:$($spec.Name)"
            continue
        }
        $labels = $null
        try {
            $labels = (($volume.Output -join '') | ConvertFrom-Json)
            Assert-EaselVolumeOwnership -Labels $labels `
                -ProjectName $ProjectName -Role $spec.Role
        }
        catch {
            Write-Output "volume=$($spec.Name) state=invalid"
            Add-Failure "volume_invalid:$($spec.Name)"
            continue
        }
        Write-Output "volume=$($spec.Name) state=present"
    }

    $control = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    $controlId = (($control.Output -join '').Trim())
    $controlImage = ''
    if ($control.ExitCode -ne 0 -or -not $controlId) {
        Write-Output 'control=missing'
        Add-Failure 'control_missing'
    }
    else {
        $state = Invoke-EaselDocker -Arguments @(
            'inspect', '--format',
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}',
            $controlId)
        $stateText = (($state.Output -join '').Trim())
        Write-Output "control=$stateText"
        if ($state.ExitCode -ne 0 -or $stateText -notmatch '^running healthy') {
            Add-Failure 'control_not_healthy'
        }
        $image = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{.Image}}', $controlId)
        $controlImage = (($image.Output -join '').Trim())
        if ($image.ExitCode -ne 0 -or
            $controlImage -notmatch '^sha256:[0-9a-f]{64}$') {
            Add-Failure 'control_image_invalid'
            $controlImage = ''
        }

        $portsText = (($(Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{json .HostConfig.PortBindings}}',
            $controlId)).Output -join '') | ConvertFrom-Json)
        $bindings = @()
        if ($null -ne $portsText) {
            $bindings = @($portsText.PSObject.Properties)
        }
        $expectedKey = "7861/tcp"
        $validPort = $false
        if ($bindings.Count -eq 1 -and
            $bindings[0].Name -eq $expectedKey) {
            $published = @($bindings[0].Value)
            $validPort = ($published.Count -eq 1 -and
                [string]$published[0].HostIp -eq '127.0.0.1' -and
                [string]$published[0].HostPort -eq [string]$Port)
        }
        if (-not $validPort) {
            Add-Failure "control_port_invalid:$Port"
        }
        Write-Output ("control_port={0} published={1}" -f $Port, $validPort)

        $mountsResult = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{json .Mounts}}', $controlId)
        if ($mountsResult.ExitCode -ne 0) {
            Add-Failure 'control_mounts_unavailable'
        }
        else {
            $mountList = @((($mountsResult.Output -join '') |
                ConvertFrom-Json))
            $byDestination = @{}
            foreach ($mount in $mountList) {
                $byDestination[[string]$mount.Destination] = $mount
            }
            if (-not $byDestination.ContainsKey('/srv/easel/control') -or
                [string]$byDestination['/srv/easel/control'].Name -ne
                    $volumes.Control -or
                -not [bool]$byDestination['/srv/easel/control'].RW) {
                Add-Failure 'control_data_mount_invalid'
            }
            if (-not $byDestination.ContainsKey('/keys') -or
                [string]$byDestination['/keys'].Name -ne $volumes.Keys -or
                [bool]$byDestination['/keys'].RW) {
                Add-Failure 'control_keys_mount_invalid'
            }
            if (-not $byDestination.ContainsKey(
                    '/run/easel/provisioner.sock') -or
                [bool]$byDestination['/run/easel/provisioner.sock'].RW) {
                Add-Failure 'control_socket_mount_invalid'
            }
            foreach ($mount in $mountList) {
                if ([string]$mount.Destination -eq '/var/run/docker.sock' -or
                    [string]$mount.Destination -like '/workspace*' -or
                    [string]$mount.Source -like '*docker.sock') {
                    Add-Failure 'control_forbidden_mount'
                }
            }
            Write-Output 'control_mounts=checked'
        }

        if ($controlImage) {
            try {
                $fingerprints = Get-EaselKeyFingerprints -Image $controlImage `
                    -KeyVolume $volumes.Keys
                $policyDigests = Get-EaselPolicyDigests -Image $controlImage `
                    -KeyVolume $volumes.Keys -EgressVolume $volumes.Egress
                $null = Assert-EaselPublicCopies `
                    -Fingerprints $fingerprints -Policy $policyDigests `
                    -PublicDir $publicDir
                Write-Output 'workspace_keys=valid'
            }
            catch {
                Add-Failure "workspace_keys_invalid:$($_.Exception.Message)"
            }
            $configCheck = Invoke-EaselDocker -Arguments @(
                'run', '--rm', '--user', '10001:10001',
                '--entrypoint', 'sh',
                '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
                '--mount', "type=volume,src=$($volumes.Egress),dst=/policy,readonly",
                $controlImage, '-c',
                ('test -s /keys/managed-agent.json && ' +
                 'test -s /keys/egress-policy.json && ' +
                 'test -s /policy/egress-policy.json && ' +
                 'test ! -e /policy/master.key && ' +
                 'test ! -e /policy/snapshot.pem && ' +
                 'test ! -e /policy/managed-agent.json'))
            if ($configCheck.ExitCode -ne 0) {
                Add-Failure 'workspace_config_invalid'
            }
            else {
                Write-Output 'workspace_config=valid'
            }
        }

        $leases = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-c',
            ("import sqlite3;c=sqlite3.connect('/srv/easel/control/control.sqlite3');" +
             "print(','.join(f'{row[0]}:{row[1]}' for row in " +
             "c.execute('select state,count(*) from worker_leases group by state')))"))
        if ($leases.ExitCode -eq 0) {
            Write-Output "worker_leases=$((($leases.Output -join '').Trim()))"
        }
        else {
            Add-Failure 'worker_leases_unavailable'
        }
        $health = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-c',
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7861/api/health',timeout=3).read()")
        if ($health.ExitCode -eq 0) {
            Write-Output 'api_health=ok'
        }
        else {
            Add-Failure 'api_health_failed'
        }
        $barrier = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-m', 'easel', 'admin',
            'barrier-status', '--control-dir', '/srv/easel/control')
        if ($barrier.ExitCode -ne 0) {
            Add-Failure 'admission_barrier_status_failed'
        }
        else {
            $barrierText = (($barrier.Output -join '').Trim())
            Write-Output "admission_barrier=$barrierText"
            if ($barrierText -and $barrierText -ne 'barrier=none') {
                Add-Failure 'admission_barrier_active'
            }
        }
    }

    $egress = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=egress')
    $egressId = (($egress.Output -join '').Trim())
    if ($egress.ExitCode -ne 0 -or -not $egressId) {
        Write-Output 'egress=missing'
        Add-Failure 'egress_missing'
    }
    else {
        $egressState = Invoke-EaselDocker -Arguments @(
            'inspect', '--format',
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}',
            $egressId)
        $egressText = (($egressState.Output -join '').Trim())
        Write-Output "egress=$egressText"
        if ($egressState.ExitCode -ne 0 -or
            $egressText -notmatch '^running healthy') {
            Add-Failure 'egress_not_healthy'
        }
        $egressPorts = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{json .HostConfig.PortBindings}}',
            $egressId)
        if ($egressPorts.ExitCode -ne 0) {
            Add-Failure 'egress_config_unavailable'
        }
        elseif (($egressPorts.Output -join '').Trim() -notin @('', 'null', '{}')) {
            Add-Failure 'egress_ports_published'
        }
        $egressMounts = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{json .Mounts}}', $egressId)
        if ($egressMounts.ExitCode -ne 0) {
            Add-Failure 'egress_mounts_unavailable'
        }
        else {
            $egressList = @((($egressMounts.Output -join '') |
                ConvertFrom-Json))
            $policyMount = $false
            foreach ($mount in $egressList) {
                if ([string]$mount.Destination -eq '/policy' -and
                    [string]$mount.Name -eq $volumes.Egress -and
                    -not [bool]$mount.RW) {
                    $policyMount = $true
                }
                if ([string]$mount.Destination -eq '/keys' -or
                    [string]$mount.Name -eq $volumes.Keys) {
                    Add-Failure 'egress_keys_mounted'
                }
            }
            if (-not $policyMount) {
                Add-Failure 'egress_policy_mount_invalid'
            }
            else {
                Write-Output 'egress_policy_mount=valid'
            }
        }
    }

    $socketCheck = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', '-lc', "test -S '$socket'")
    if ($socketCheck.ExitCode -eq 0) {
        Write-Output 'provisioner_socket=present'
        $expectedFile = Join-Path (Join-Path $DataRoot 'logs') `
            'provisioner-expected.json'
        $engineIdentity = $null
        $socketIdentity = $null
        try {
            $engineIdentity = Get-EaselEngineIdentity
            $socketIdentity = Get-EaselSocketIdentity
            Write-Output ("provisioner_engine={0}/{1}" -f `
                $engineIdentity.Context, $engineIdentity.Id)
        }
        catch {
            Add-Failure "provisioner_engine_unverifiable:$($_.Exception.Message)"
        }
        if (-not $controlImage) {
            Add-Failure 'provisioner_identity_image_unavailable'
        }
        elseif (-not (Test-Path -LiteralPath $expectedFile -PathType Leaf)) {
            Add-Failure 'provisioner_expected_missing'
        }
        else {
            try {
                $expected = Get-Content -LiteralPath $expectedFile -Raw |
                    ConvertFrom-Json
            }
            catch {
                $expected = $null
            }
            if ($null -eq $expected -or
                -not ($expected.PSObject.Properties.Name -contains 'engineId') -or
                -not ($expected.PSObject.Properties.Name -contains 'engineContext') -or
                -not ($expected.PSObject.Properties.Name -contains 'socketUid')) {
                Add-Failure 'provisioner_expected_incomplete'
            }
            elseif ($null -ne $engineIdentity -and (
                    [string]$expected.engineId -ne $engineIdentity.Id -or
                    [string]$expected.engineContext -ne $engineIdentity.Context)) {
                Add-Failure 'provisioner_engine_drift'
            }
            elseif ($null -ne $socketIdentity -and (
                    [int]$expected.socketUid -ne $socketIdentity.Uid -or
                    [int]$expected.socketGid -ne $socketIdentity.Gid -or
                    [string]$expected.socketMode -ne '660')) {
                Add-Failure 'provisioner_socket_identity_drift'
            }
            $identity = Invoke-ProvisionerIdentityCheck -SocketPath $socket `
                -ExpectedFile $expectedFile -Image $controlImage
            if ($identity.ExitCode -eq 0) {
                Write-Output 'provisioner_identity=ok'
            }
            elseif ($identity.ExitCode -eq 3) {
                Add-Failure ("provisioner_identity_mismatch:" +
                    (($identity.Output -join ',').Trim()))
            }
            else {
                Add-Failure 'provisioner_identity_unavailable'
            }
        }
    }
    else {
        Write-Output 'provisioner_socket=missing'
        Add-Failure 'provisioner_socket_missing'
    }

    $deploymentFile = Join-Path (Join-Path $DataRoot 'logs') 'deployment.json'
    if (Test-Path -LiteralPath $deploymentFile -PathType Leaf) {
        try {
            $deployment = Get-Content -LiteralPath $deploymentFile -Raw |
                ConvertFrom-Json
            Write-Output "version_control_image=$([string]$deployment.control_image)"
            Write-Output "version_worker_image=$([string]$deployment.worker_image)"
            Write-Output "version_gateway_image=$([string]$deployment.gateway_image)"
            Write-Output "version_frontend_sha256=$([string]$deployment.frontend_sha256)"
            Write-Output "version_config_sha256=$([string]$deployment.config_sha256)"
            Write-Output "version_source_commit=$([string]$deployment.source_commit)"
            Write-Output "version_harness_revision=$([string]$deployment.harness_revision)"
            Write-Output "version_project=$([string]$deployment.project)"
        }
        catch {
            Add-Failure 'deployment_metadata_invalid'
        }
    }
    else {
        Write-Output 'version_metadata=missing'
    }
    Write-Output "logs=$(Join-Path $DataRoot 'logs')"
    if ($controlId) {
        $schema = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-c',
            ("import sqlite3;print(sqlite3.connect(" +
             "'/srv/easel/control/control.sqlite3').execute(" +
             "'PRAGMA user_version').fetchone()[0])"))
        $schemaText = (($schema.Output -join '').Trim())
        if ($schema.ExitCode -eq 0 -and $schemaText -match '^[0-9]+$') {
            Write-Output "version_schema=$schemaText"
        }
        else {
            Add-Failure 'schema_version_unavailable'
        }
    }

    if ($script:failures.Count -gt 0) {
        Write-Output ("status=failed failures=" + ($script:failures -join ','))
        exit 1
    }
    Write-Output 'status=ok'
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
