# The schema runbook_assistant in the workspace's catalog: the pages table and
# the chunk tables the chunker job writes, the managed volume packages for
# the corpus it reads, the experiment the evaluations log to, and the SQL
# warehouse Catalog Explorer reads the pages table with. None of these bills
# while idle: the warehouse stops after 5 minutes without a query.

resource "databricks_schema" "assistant" {
  catalog_name = local.catalog
  name         = local.databricks.schema
  comment      = "runbook-assistant (demo 23): the pages, the chunk tables and the corpus volume."
}

resource "databricks_volume" "packages" {
  catalog_name = local.catalog
  schema_name  = databricks_schema.assistant.name
  name         = local.databricks.volume
  volume_type  = "MANAGED"
  comment      = "Each package's corpus, by SHA-256, for the chunker job."
}

# The pipeline's identity in the workspace: no admin, no clusters. If the
# workspace refuses to make it, add it in Settings, Identity and access, and
# import it: scripts/demos/rag/tf.sh import databricks_service_principal.pipeline <id>
resource "databricks_service_principal" "pipeline" {
  display_name          = "release-pipeline"
  allow_cluster_create  = false
  databricks_sql_access = true
  workspace_access      = true
}

# Per principal, so the grants of everyone else on the catalog and schema stay.
resource "databricks_grant" "catalog" {
  catalog    = local.catalog
  principal  = databricks_service_principal.pipeline.application_id
  privileges = ["USE_CATALOG"]
}

resource "databricks_grant" "schema" {
  schema     = databricks_schema.assistant.id
  principal  = databricks_service_principal.pipeline.application_id
  privileges = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE", "READ_VOLUME", "WRITE_VOLUME"]
}

# One evaluation run per release, 40 fixed questions each.
resource "databricks_mlflow_experiment" "assistant" {
  name = local.databricks.experiment
}

resource "databricks_permissions" "experiment" {
  experiment_id = databricks_mlflow_experiment.assistant.id
  access_control {
    service_principal_name = databricks_service_principal.pipeline.application_id
    permission_level       = "CAN_EDIT"
  }
}

resource "databricks_sql_endpoint" "assistant" {
  name                      = local.databricks.warehouse
  cluster_size              = "2X-Small"
  min_num_clusters          = 1
  max_num_clusters          = 1
  auto_stop_mins            = 5
  enable_serverless_compute = true
  warehouse_type            = "PRO"
}
