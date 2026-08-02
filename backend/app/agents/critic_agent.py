import json
import logging
from typing import Dict, Any, List

from app.tools.validation_tools import validate_changes

logger = logging.getLogger(__name__)

class CriticAgent:
    """
    Runs the 5-gate validation pipeline.
    It's essentially a wrapper around the validation_tools, adapted for the LangGraph workflow.
    """
    def __init__(self, project_root: str):
        self.project_root = project_root

    def validate(self, changes: List[Dict[str, str]]) -> Dict[str, Any]:
        """Run validation gates and return the report."""
        logger.info("🕵️ Critic Agent running validation pipeline...")
        
        # We invoke the tool function directly since it's just code
        raw_report = validate_changes(changes, self.project_root)
        
        try:
            report = json.loads(raw_report)
            return report
        except Exception as e:
            logger.error(f"Failed to parse validation report: {e}\n{raw_report}")
            return {
                "overall": "ERROR",
                "gates": {
                    "syntax": {"status": "ERROR", "details": f"Failed to parse report: {e}"}
                }
            }
