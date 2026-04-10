"""Build podFindAndDeployOnDemand mutation with safe escaping for dockerArgs and env."""

from __future__ import annotations

from typing import Any

from runpod.api.graphql import run_graphql_query


def _gq_str(s: str) -> str:
    """Escape a string for use inside GraphQL double-quoted string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def deploy_gpu_pod(
    *,
    name: str,
    image_name: str,
    gpu_type_id: str,
    cloud_type: str,
    support_public_ip: bool,
    start_ssh: bool,
    data_center_id: str | None,
    gpu_count: int,
    container_disk_in_gb: int,
    ports: str,
    env: dict[str, str],
    docker_args: str,
    min_vcpu_count: int = 2,
    cpu_workload: bool = False,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Send ``podFindAndDeployOnDemand`` with properly escaped ``dockerArgs``.

    RunPod requires ``gpuTypeId`` even for CPU-only pods. For ``cpu_workload``, pass
    ``computeType: CPU``, ``gpuCount: 0`` (from ``gpu_count``), ``minVcpuCount``, and a
    bootstrap ``gpu_type_id`` resolved from ``resources.bootstrap_gpu``.
    """
    fields: list[str] = [
        f'name: "{_gq_str(name)}"',
        f'imageName: "{_gq_str(image_name)}"',
        f"cloudType: {cloud_type}",
        "startSsh: true" if start_ssh else "startSsh: false",
        f'gpuTypeId: "{_gq_str(gpu_type_id)}"',
    ]
    if cpu_workload:
        fields.extend(
            [
                "computeType: CPU",
                f"minVcpuCount: {min_vcpu_count}",
                f"gpuCount: {gpu_count}",
                f"supportPublicIp: {str(support_public_ip).lower()}",
            ]
        )
    else:
        fields.extend(
            [
                f"supportPublicIp: {str(support_public_ip).lower()}",
                f"gpuCount: {gpu_count}",
            ]
        )

    fields.extend(
        [
            f"containerDiskInGb: {container_disk_in_gb}",
            f'dataCenterId: "{_gq_str(data_center_id)}"'
            if data_center_id is not None
            else "dataCenterId: null",
            f'ports: "{_gq_str(ports.replace(" ", ""))}"',
            f'dockerArgs: "{_gq_str(docker_args)}"',
        ]
    )
    env_items = [f'{{ key: "{_gq_str(k)}", value: "{_gq_str(v)}" }}' for k, v in env.items()]
    fields.append(f"env: [{', '.join(env_items)}]")

    input_string = ", ".join(fields)
    query = f"""
    mutation {{
      podFindAndDeployOnDemand(
        input: {{
          {input_string}
        }}
      ) {{
        id
        imageName
        env
        machineId
        machine {{
          podHostId
        }}
      }}
    }}
    """
    raw = run_graphql_query(query, api_key=api_key)
    return raw["data"]["podFindAndDeployOnDemand"]
