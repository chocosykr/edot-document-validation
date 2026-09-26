# Registry Tool (`./reg`)

A CLI tool to view, inspect, and manage validation methods in the DVS method registry.

## Commands

### List methods
```bash
./reg list                        # List all methods
./reg list --status active        # Filter by status (ACTIVE, INACTIVE, UNHEALTHY, TESTING, DEGRADED)
./reg list --country India        # Filter by country
./reg list --doc-type SID         # Filter by document type
./reg list --country India --status active   # Combine filters
```

### Show method details
```bash
./reg show M_IND_SID_001          # Show full JSON config of a method
```

### Delete methods
```bash
./reg delete M_MYANMAR_SID_API    # Delete a specific method
./reg delete ALL                  # Delete ALL methods (nuclear option)
```

### Cleanup
```bash
./reg cleanup                     # Delete all UNHEALTHY and INACTIVE methods
```

## Aliases

| Full command | Shortcut |
|---|---|
| `./reg list` | `./reg ls` |
| `./reg delete` | `./reg rm` |

## Method Statuses

| Status | Meaning |
|---|---|
| `ACTIVE` | Working, used in production validation |
| `TESTING` | Newly generated, being validated |
| `DEGRADED` | Partially working (e.g., intermittent failures) |
| `UNHEALTHY` | Failed validation, not used |
| `INACTIVE` | Disabled (outdated schema or manually deactivated) |
