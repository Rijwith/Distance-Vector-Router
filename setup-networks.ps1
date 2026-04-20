$networks = @(
    @{ Name = "net_ab"; Subnet = "10.0.1.0/24"; Gateway = "10.0.1.254" },
    @{ Name = "net_bc"; Subnet = "10.0.2.0/24"; Gateway = "10.0.2.254" },
    @{ Name = "net_ac"; Subnet = "10.0.3.0/24"; Gateway = "10.0.3.254" }
)

foreach ($network in $networks) {
    $exists = docker network ls --format "{{.Name}}" | Select-String -SimpleMatch $network.Name
    if (-not $exists) {
        docker network create --subnet=$($network.Subnet) --gateway=$($network.Gateway) $($network.Name)
    }
    else {
        Write-Host "$($network.Name) already exists"
    }
}
