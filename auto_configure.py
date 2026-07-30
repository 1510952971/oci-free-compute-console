#!/usr/bin/env python3
"""Discover a regional subnet and current ARM/AMD images, then write config."""

from __future__ import annotations

import os
from pathlib import Path

import oci


def main() -> int:
    config = oci.config.from_file()
    tenancy = config["tenancy"]
    identity = oci.identity.IdentityClient(config)
    network = oci.core.VirtualNetworkClient(config)
    compute = oci.core.ComputeClient(config)
    subnets = oci.pagination.list_call_get_all_results(
        network.list_subnets, tenancy, lifecycle_state="AVAILABLE"
    ).data
    regional = [item for item in subnets if item.availability_domain is None]
    if not regional:
        raise RuntimeError("No regional subnet found in the root compartment")
    subnet = regional[0]
    def latest_image(shape: str):
        images = compute.list_images(
            tenancy,
            shape=shape,
            operating_system="Canonical Ubuntu",
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        if not images:
            raise RuntimeError(f"No Ubuntu image found for {shape} in the home region")
        return sorted(images, key=lambda item: item.time_created or "", reverse=True)[0]

    arm_image = latest_image("VM.Standard.A1.Flex")
    micro_image = latest_image("VM.Standard.E2.1.Micro")
    ads = [item.name for item in identity.list_availability_domains(tenancy).data]
    ssh = Path.home() / ".ssh" / "id_ed25519.pub"
    if not ssh.exists():
        raise RuntimeError(f"SSH public key not found: {ssh}")
    text = f'''# Auto-generated from your OCI home region. Review if needed.
region = "{config["region"]}"
compartment_id = "{tenancy}"
subnet_id = "{subnet.id}"
arm_image_id = "{arm_image.id}"
micro_image_id = "{micro_image.id}"
image_operating_system = "Canonical Ubuntu"
image_operating_system_version = ""
ssh_public_key_file = "{ssh}"
display_name_prefix = "free-oci"
availability_domains = {ads!r}
assign_public_ip = true
default_preset = "arm_full"
retry_seconds = 120
jitter_seconds = 60
warmup_seconds = 60
warmup_jitter_seconds = 30
warmup_attempt_limit = 12
daily_attempt_limit = 720
oci_config_file = "~/.oci/config"
oci_profile = "DEFAULT"

# Optional notifications. Keep empty to use macOS notifications only.
pushplus_token = ""
pushplus_topic = ""
telegram_bot_token = ""
telegram_chat_id = ""
notification_proxy = ""
'''
    Path("config.toml").write_text(text, encoding="utf-8")
    print(
        f"Configured {config['region']} with subnet {subnet.display_name}, "
        f"ARM image {arm_image.display_name}, and AMD image {micro_image.display_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
