# Copyright 2019 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import mlrun
from kfp import dsl

from mlrun.model import HyperParamOptions


# Create a Kubeflow Pipelines pipeline
@dsl.pipeline(
    name="Fraud Detection Pipeline",
    description="Detecting fraud from a transactions dataset",
)
def pipeline(vector_name="transactions-fraud", features=[], label_column="is_error"):
    """
    add the doc string
    """
    
    # Get the project
    project = mlrun.get_current_project()  

    # Get FeatureVector
    get_vector_func = project.get_function("get-vector")
    get_vector_run = project.run_function(
        get_vector_func,
        name="get-vector",
        inputs={
            "feature_vector": vector_name,
            "features": features,
            "label_feature": label_column,
            "target": {"name": "parquet", "kind": "parquet"},
            "update_stats": True,
        },
        outputs = [
            "feature_vector", "target"
        ]
        #returns = [
        #    "feature_vector: dataset", "target: dataset"
        #    ]
    )

    # Feature selection
    feature_selection_func = project.get_function("feature-selection")
    feature_selection_run = project.run_function(
        feature_selection_func,
        name="feature-selection",
        params={
            "output_vector_name": "short",
            "label_column": project.get_param("label_column", "label"),
            "k": 18,
            "min_votes": 2,
            "ignore_type_errors": True,
        },
        inputs={
            "df_artifact": project.get_artifact_uri(
                get_vector_run.outputs["feature_vector"], "feature-vector"
            )
        },
        outputs=[
            "feature_scores",
            "selected_features_count",
            "top_features_vector",
            "selected_features",
        ],
    ).after(get_vector_run)

    # train with hyper-paremeters
    train_func = project.get_function("train")
    train_run = project.run_function(
        train_func,
        name="train",
        handler="train",
        params={
            "sample": -1,
            "label_column": project.get_param("label_column", "label"),
            "test_size": 0.10,
        },
        hyperparams={
            "model_name": [
                "transaction_fraud_rf",
                "transaction_fraud_xgboost",
                "transaction_fraud_adaboost",
            ],
            "model_class": [
                "sklearn.ensemble.RandomForestClassifier",
                "sklearn.linear_model.LogisticRegression",
                "sklearn.ensemble.AdaBoostClassifier",
            ],
        },
        hyper_param_options=HyperParamOptions(
            strategy="list", selector="max.accuracy"
        ),
        inputs={"dataset": feature_selection_run.outputs["top_features_vector"]},
        outputs=["model", "test_set"],
    ).after(feature_selection_run)

    # test and visualize your model
    test_func = project.get_function("evaluate")
    test_run = mlrun.run_function(
        test_func,
        name="evaluate",
        handler="evaluate",
        params={
            "label_columns": project.get_param("label_column", "label"),
            "model": train.outputs["model"],
            "drop_columns": project.get_param("label_column", "label"),
        },
        inputs={"dataset": train.outputs["test_set"]},
    ).after(train_run)

    # Create a serverless function from the hub, add a feature enrichment router
    # This will enrich and impute the request with data from the feature vector
    serving_func = project.get_function("serving")
    serving_func.set_topology(
        "router",
        mlrun.serving.routers.EnrichmentModelRouter(
            feature_vector_uri="short", impute_policy={"*": "$mean"}
        ),
        exist_ok=True,
    )
    # Enable model monitoring
    serving_func.set_tracking()
    serving_func.save()
    # deploy the model server, pass a list of trained models to serve
    deploy = project.deploy_function(
        serving_func,
        models=[{"key": "fraud", "model_path": train.outputs["model"]}],
    ).after(train_run)
