"""TEST-ONLY provisioning helpers.

Read a prerequisite inventory (prerequisite/<env>-env/inventory.json) and either
**check** whether the declared identities/objects exist in the workspace, or
**create** them (create is gated behind an explicit flag).

This is NOT part of the governance framework — it exists to stand up / tear-check a
test workspace. The framework itself never provisions resources.
"""
