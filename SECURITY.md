# Security policy

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/dcro/daren2mqtt/security/advisories/new),
not in public issues. You should get a reply within a week.

Only the latest release is supported.

daren2mqtt only sends read requests to the BMS. It keeps no data of its own and listens on
no network port: it connects out to the RS485 gateways and to the MQTT broker.
