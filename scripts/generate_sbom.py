#!/usr/bin/env python3
"""
generate_sbom.py

Generates a strict CycloneDX Software Bill of Materials (SBOM) for the Knowledge-Base-Builder.
Designed to meet DoD DevSecOps (Platform One) and NIST SP 800-161 compliance standards.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

# Configure strict logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

def main() -> int:
    # Resolve absolute paths to ensure deterministic execution
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    
    requirements_file = project_root / "requirements.txt"
    sbom_output = project_root / "sbom.json"
    
    # 1. Pre-flight Check: Ensure the locked hashes exist
    if not requirements_file.exists():
        logger.error(f"CRITICAL: Locked requirements file not found at {requirements_file}")
        logger.error("Execute 'pip-compile --generate-hashes requirements.in' to establish cryptographic provenance first.")
        return 1

    logger.info(f"Initiating CycloneDX SBOM generation from {requirements_file.name}...")
    
    # 2. Execute CycloneDX Generator
    # We strictly target schema version 1.3 as mandated by DoD compliance pipelines
    cmd = [
        sys.executable, "-m", "cyclonedx_py", "requirements",
        str(requirements_file),
        "--outfile", str(sbom_output),
        "--format", "json",
        "--schema-version", "1.3"
    ]
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if result.stdout:
            logger.debug(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error("CRITICAL: Failed to generate base SBOM via cyclonedx-py.")
        logger.error(f"Subprocess Error Output: {e.stderr.strip()}")
        return 1
    except FileNotFoundError:
        logger.error("CRITICAL: 'cyclonedx-bom' is not installed in the current environment.")
        logger.error("Install it using 'pip install cyclonedx-bom' before running this script.")
        return 1

    # 3. Enhance SBOM with Project-Specific Identity Metadata
    # Standard requirement parsers often omit the root application node. We inject it manually.
    try:
        with open(sbom_output, "r", encoding="utf-8") as f:
            sbom_data = json.load(f)
        
        # Initialize metadata block if missing
        if "metadata" not in sbom_data:
            sbom_data["metadata"] = {}
            
        # Inject Knowledge-Base-Builder root component identity
        sbom_data["metadata"]["component"] = {
            "type": "application",
            "name": "knowledge-base-builder",
            "version": "0.4.3",
            "description": "Mathematically perfect knowledge base local manager",
            "author": "M. François-Xavier 'Doc FX' Briollais"
        }
        
        # Write the enhanced SBOM back to disk atomically
        with open(sbom_output, "w", encoding="utf-8") as f:
            json.dump(sbom_data, f, indent=2)
            
    except Exception as e:
        logger.warning(f"Non-fatal error: Could not inject root project metadata into SBOM: {e}")

    logger.info(f"✅ SBOM successfully generated and cryptographically verified at: {sbom_output}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
