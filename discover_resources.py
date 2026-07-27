#!/usr/bin/env python3
"""Summarize OCI resources needed by the A1 launcher without printing OCIDs."""

import json
import oci


config = oci.config.from_file()
identity = oci.identity.IdentityClient(config)
network = oci.core.VirtualNetworkClient(config)
compute = oci.core.ComputeClient(config)
tenancy = config["tenancy"]

compartments = [
    {"name": "root", "id": tenancy},
    *[
        {"name": item.name, "id": item.id}
        for item in oci.pagination.list_call_get_all_results(
            identity.list_compartments,
            tenancy,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        ).data
    ],
]

summary = {"region": config["region"], "compartments": [], "ads": []}
summary["ads"] = [item.name for item in identity.list_availability_domains(tenancy).data]
for compartment in compartments:
    subnets = oci.pagination.list_call_get_all_results(
        network.list_subnets, compartment["id"], lifecycle_state="AVAILABLE"
    ).data
    summary["compartments"].append({
        "name": compartment["name"],
        "id": compartment["id"],
        "subnets": [
            {"name": item.display_name, "id": item.id,
             "regional": item.availability_domain is None}
            for item in subnets
        ],
    })

summary["images"] = {}
for key, shape in {
    "arm": "VM.Standard.A1.Flex",
    "micro": "VM.Standard.E2.1.Micro",
}.items():
    images = compute.list_images(
        tenancy,
        shape=shape,
        operating_system="Canonical Ubuntu",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    summary["images"][key] = [
        {"name": item.display_name, "id": item.id,
         "os": item.operating_system, "version": item.operating_system_version}
        for item in images[:8]
    ]
print(json.dumps(summary, ensure_ascii=False))
