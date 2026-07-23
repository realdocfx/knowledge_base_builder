#!/usr/bin/env python3
"""
Generate CycloneDX Software Bill of Materials (SBOM) from requirements.txt

This script generates a CycloneDX SBOM from the pip requirements.txt file
with SHA-256 hashes, which is required for MIL-SPEC compliance and DoD DevSecOps.
"""

import json
import re
import sys
from pathlib import Path

try:
    from cyclonedx.model import bom
    from cyclonedx.model.component import Component, ComponentType
    from cyclonedx.model import HashType, HashAlgorithm
    from cyclonedx.output.json import JsonV1Dot3
except ImportError as e:
    print(f"ERROR: Required cyclonedx packages not installed: {e}")
    print("Run: pip install cyclonedx-bom")
    sys.exit(1)


def parse_requirements_with_hashes(requirements_path: Path) -> list:
    """Parse requirements.txt and extract package info with hashes."""
    packages = []
    current_package = None
    
    with open(requirements_path, 'r') as f:
        for line in f:
            line = line.strip()
            
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            
            # Check for package line
            if '==' in line and not line.startswith('--'):
                if current_package:
                    packages.append(current_package)
                
                # Parse package name and version
                parts = line.split('==')
                name = parts[0].strip()
                version = parts[1].split()[0].strip() if len(parts) > 1 else ""
                current_package = {
                    'name': name,
                    'version': version,
                    'hashes': []
                }
            
            # Check for hash line
            elif line.startswith('--hash=') and current_package:
                hash_value = line.split('=', 1)[1].strip()
                if hash_value:
                    current_package['hashes'].append(hash_value)
    
    # Don't forget the last package
    if current_package:
        packages.append(current_package)
    
    return packages


def main():
    """Generate SBOM from requirements.txt."""
    repo_root = Path(__file__).resolve().parents[1]
    requirements_txt = repo_root / "requirements.txt"
    sbom_json = repo_root / "sbom.json"
    
    if not requirements_txt.exists():
        print(f"ERROR: {requirements_txt} not found. Run pip-compile first.")
        sys.exit(1)
    
    print(f"Parsing requirements from {requirements_txt}...")
    packages = parse_requirements_with_hashes(requirements_txt)
    
    print(f"Found {len(packages)} packages with hash information")
    
    # Create CycloneDX BOM
    bom_obj = bom.Bom()
    
    # Add components for each package
    for pkg in packages:
        component = Component(
            name=pkg['name'],
            version=pkg['version'],
            type=ComponentType.LIBRARY
        )
        
        # Add hashes if available
        if pkg['hashes']:
            for hash_value in pkg['hashes']:
                # Parse hash format: algorithm:hash
                if ':' in hash_value:
                    algo, h = hash_value.split(':', 1)
                    if algo == 'sha256':
                        hash_obj = HashType(
                            alg=HashAlgorithm.SHA_256,
                            content=h
                        )
                        component.hashes.add(hash_obj)
        
        bom_obj.components.add(component)
    
    # Generate JSON output
    output = JsonV1Dot3(bom_obj)
    sbom_content = output.output_as_string(indent=2)
    
    # Write SBOM file
    with open(sbom_json, 'w') as f:
        f.write(sbom_content)
    
    print(f"SBOM generated successfully: {sbom_json}")
    print(f"Components: {len(bom_obj.components)}")


if __name__ == "__main__":
    main()
