import logging
import subprocess
from eo4eu_api_utils import Client, create_uc5

logger = logging.getLogger("test")
logging.basicConfig()
logger.setLevel(logging.DEBUG)

development =  'https://umm-api.dev.wekeo.apps.eo4eu.eu'
username_dev = subprocess.check_output(["pass", "eo4eu/openeo-username-dev"], text=True).strip()
password_dev = subprocess.check_output(["pass", "eo4eu/openeo-password-dev"], text=True).strip()

if __name__ == "__main__":
    client = Client(development, username_dev, password_dev)
    status = "PUBLISHING"
    cfs = "FALSE"
    workflows = client.list_workflows(status, cfs)
    print(workflows)

    workflow_name = "test-uc5"
    start_date = "2024-01-04T22:00:00.000Z"
    end_date = "2024-01-05T22:00:00.000Z"
    geometry = "[[[11.761812,36.203504],[11.761812,38.517227],[15.957268,38.517227],[15.957268,36.203504]]]"
    id = create_uc5(client, workflow_name, start_date, end_date, geometry)
    print(id)
