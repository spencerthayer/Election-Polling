import pandas as pd
import requests
from datetime import datetime
from io import StringIO
import numpy as np
from typing import Dict, List
from states import get_state_data
from scipy.stats import norm
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

# Download the Data
polling_url = "https://projects.fivethirtyeight.com/polls/data/president_polls.csv"
favorability_url = "https://projects.fivethirtyeight.com/polls/data/favorability_polls.csv"

# Data Parsing
candidate_names = ['Joe Biden', 'Donald Trump']
favorability_weight = 0.1
heavy_weight = True

# Coloring
start_color = 164
skip_color = 3

# Define the time decay weighting
decay_rate = 2
half_life_days = 28

# Constants for the weighting calculations
grade_weights = {
    'A+': 1.0, 'A': 0.9, 'A-': 0.8, 'A/B': 0.75, 'B+': 0.7,
    'B': 0.6, 'B-': 0.5, 'B/C': 0.45, 'C+': 0.4, 'C': 0.3,
    'C-': 0.2, 'C/D': 0.15, 'D+': 0.1, 'D': 0.05, 'D-': 0.025
}
partisan_weight = {True: 0.1, False: 1}
population_weights = {
    'lv': 1.0, 'rv': 0.6666666666666666, 'v': 0.5,
    'a': 0.3333333333333333, 'all': 0.3333333333333333
}

def margin_of_error(n, p=0.5, confidence_level=0.95):
    z = norm.ppf((1 + confidence_level) / 2)
    moe = z * np.sqrt((p * (1 - p)) / n)
    return moe * 100  # Convert to percentage

def download_csv_data(url: str) -> pd.DataFrame:
    """
    Download CSV data from the specified URL.
    """
    try:
        response = requests.get(url)
        response.raise_for_status()
        csv_data = StringIO(response.content.decode('utf-8'))
        return pd.read_csv(csv_data)
    except (requests.RequestException, pd.errors.EmptyDataError, ValueError) as e:
        print(f"Error downloading data from {url}: {e}")
        return pd.DataFrame()

def preprocess_data(df: pd.DataFrame, start_period: pd.Timestamp = None) -> pd.DataFrame:
    """
    Preprocess the data by converting date columns, handling missing values, filtering irrelevant data,
    and normalizing numeric_grade, pollscore, and transparency_score.
    """
    df['created_at'] = pd.to_datetime(df['created_at'], format='%m/%d/%y %H:%M', errors='coerce')
    df = df.dropna(subset=['created_at'])
    if start_period is not None:
        df = df[df['created_at'] >= start_period]

    # Normalizing numeric_grade
    df['numeric_grade'] = pd.to_numeric(df['numeric_grade'], errors='coerce').fillna(0)
    max_numeric_grade = df['numeric_grade'].max()
    df['normalized_numeric_grade'] = df['numeric_grade'] / max_numeric_grade

    # Inverting and normalizing pollscore
    df['pollscore'] = pd.to_numeric(df['pollscore'], errors='coerce')  # Ensure pollscore is float
    min_pollscore = df['pollscore'].min()
    max_pollscore = df['pollscore'].max()
    df['normalized_pollscore'] = 1 - (df['pollscore'] - min_pollscore) / (max_pollscore - min_pollscore)

    # Normalize transparency_score
    df['transparency_score'] = pd.to_numeric(df['transparency_score'], errors='coerce').fillna(0)
    max_transparency_score = df['transparency_score'].max()
    df['normalized_transparency_score'] = df['transparency_score'] / max_transparency_score

    # Clip the normalized values to ensure they are within [0, 1] range
    df['normalized_numeric_grade'] = df['normalized_numeric_grade'].clip(0, 1)
    df['normalized_pollscore'] = df['normalized_pollscore'].clip(0, 1)
    df['normalized_transparency_score'] = df['normalized_transparency_score'].clip(0, 1)

    # Calculate time decay weight
    reference_date = pd.Timestamp.now()
    days_old = (reference_date - df['created_at']).dt.days
    df['time_decay_weight'] = np.exp(-np.log(decay_rate) * days_old / half_life_days)

    # Calculate transparency weight
    df['transparency_weight'] = df['transparency_score'] / max_transparency_score

    # Calculate population weight
    if 'population_weight' not in df.columns:
        if 'population' in df.columns:
            df.loc[:, 'population'] = df['population'].str.lower()
            df.loc[:, 'population_weight'] = df['population'].map(lambda x: population_weights.get(x, 1))
        else:
            print("Warning: 'population' column is missing. Setting 'population_weight' to 1 for all rows.")
            df.loc[:, 'population_weight'] = 1

    # Calculate partisan weight
    df.loc[:, 'is_partisan'] = df['partisan'].notna() & df['partisan'].ne('')
    df.loc[:, 'partisan_weight'] = df['is_partisan'].map(partisan_weight)

    return df

def calculate_combined_weight(df: pd.DataFrame, state_data: dict) -> pd.DataFrame:
    """
    Calculate and apply combined weights to the dataframe.

    Args:
    df: Dataframe containing the polling or favorability data.
    state_data: Dictionary containing state rank data.

    Returns:
    A dataframe with an additional 'combined_weight' column.
    """
    # Assuming necessary columns like 'time_decay_weight', 'sample_size', etc., are already present in df

    # Sample size normalization
    min_sample_size, max_sample_size = df['sample_size'].min(), df['sample_size'].max()
    df['sample_size_weight'] = (df['sample_size'] - min_sample_size) / (max_sample_size - min_sample_size)

    # State rank weight
    df['state_rank'] = df['state'].apply(lambda x: state_data.get(x, 1))

    # Combine the weights
    list_weights = np.array([
        df['time_decay_weight'],
        df['sample_size_weight'],
        df['transparency_weight'],  # Assume this column is pre-calculated
        df['population_weight'],    # This too
        df['partisan_weight'],      # And this
        df['state_rank'],
        df['normalized_numeric_grade'],
        df['normalized_pollscore'],
        df['normalized_transparency_score']
    ])
    df['combined_weight'] = np.prod(list_weights, axis=0)
    
    return df

def calculate_timeframe_specific_moe(df, candidate_names):
    moes = []
    for candidate in candidate_names:
        candidate_df = df[df['candidate_name'] == candidate]
        if candidate_df.empty:
            # Skip this candidate if there are no polls
            continue
        for _, poll in candidate_df.iterrows():
            if poll['sample_size'] > 0 and 0 <= poll['pct'] <= 100:  # Ensuring sample size and percentage are valid
                moe = margin_of_error(n=poll['sample_size'], p=poll['pct']/100)
                moes.append(moe)
    return np.mean(moes) if moes else np.nan  # Only return a mean if there are MOE values to average

def calculate_polling_metrics(df: pd.DataFrame, candidate_names: List[str], state_data: dict) -> Dict[str, float]:
    """
    Calculate polling metrics for the specified candidate names.
    Ensure percentages are handled correctly.
    """
    df = calculate_combined_weight(df, state_data)  # Replace direct weight calculations
    
    # Rest of the function remains unchanged...

def calculate_favorability_differential(df: pd.DataFrame, candidate_names: List[str], state_data: dict) -> Dict[str, float]:
    """
    Calculate favorability differentials for the specified candidate names.
    Ensure percentages are handled correctly.
    """
    df = calculate_combined_weight(df, state_data)  # Replace direct weight calculations

    # Rest of the function remains unchanged...

def combine_analysis(polling_metrics: Dict[str, float], favorability_differential: Dict[str, float], favorability_weight: float) -> Dict[str, float]:
    """
    Combine polling metrics and favorability differentials into a unified analysis.
    """
    combined_metrics = {}
    for candidate in polling_metrics.keys():
        combined_metrics[candidate] = (
            polling_metrics[candidate][0] * (1 - favorability_weight) + favorability_differential[candidate] * favorability_weight,
            polling_metrics[candidate][1]
        )
    return combined_metrics

def print_with_color(text: str, color_code: int):
    """
    Print text with the specified color code using ANSI escape sequences.
    """
    print(f"\033[38;5;{color_code}m{text}\033[0m")

def output_results(combined_results: Dict[str, float], color_index: int, period_value: int, period_type: str, oob_variance: float):
    """
    Corrected output formatting to display percentages properly and include OOB variance.
    """
    biden_score, biden_margin = combined_results['Joe Biden']
    trump_score, trump_margin = combined_results['Donald Trump']
    differential = trump_score - biden_score
    favored_candidate = "Biden" if differential < 0 else "Trump"
    color_code = start_color + (color_index * skip_color)
    print(f"\033[38;5;{color_code}m{period_value:2d}{period_type[0]:<4} B∙{biden_score:5.2f}%±{biden_margin:.2f} T∙{trump_score:5.2f}%±{trump_margin:.2f} {abs(differential):+5.2f} {favored_candidate} 𝛂{oob_variance:5.1f}\033[0m")

def _get_unsampled_indices(tree, n_samples):
    # Get the indices of the OOB samples for the given tree
    unsampled_mask = np.ones(n_samples, dtype=bool)
    unsampled_mask[tree.tree_.feature[tree.tree_.feature >= 0]] = False
    return np.arange(n_samples)[unsampled_mask]

def main():
    polling_df = download_csv_data(polling_url)
    favorability_df = download_csv_data(favorability_url)

    polling_df = preprocess_data(polling_df)
    favorability_df = preprocess_data(favorability_df)

    min_samples_required = 5
    n_trees = 1000

    # Get state rank data before the loop
    state_data = get_state_data()  # Assuming get_state_data() is defined in the 'states' module

    color_index = 0
    for period in [(12, 'months'), (6, 'months'), (3, 'months'), (1, 'months'), (21, 'days'), (14, 'days'), (7, 'days'), (3, 'days'), (1, 'days')]:
        period_value, period_type = period
        if period_type == 'months':
            start_period = pd.Timestamp.now() - pd.DateOffset(months=period_value)
        elif period_type == 'days':
            start_period = pd.Timestamp.now() - pd.Timedelta(days=period_value)

        filtered_polling_df = preprocess_data(polling_df[(polling_df['created_at'] >= start_period) &
                                                          (polling_df['candidate_name'].isin(candidate_names))].copy(), start_period)
        filtered_favorability_df = preprocess_data(favorability_df[(favorability_df['created_at'] >= start_period) &
                                                                    (favorability_df['politician'].isin(candidate_names))].copy(), start_period)

        if filtered_favorability_df.empty:
            print_with_color(f"No data available for {period_value} {period_type}.", color_index)
            color_index += 1
            continue  # Skip to the next period

        polling_metrics = calculate_polling_metrics(filtered_polling_df, candidate_names, state_data)
        favorability_differential = calculate_favorability_differential(filtered_favorability_df, candidate_names, state_data)

        combined_results = combine_analysis(polling_metrics, favorability_differential, favorability_weight)

        features_columns = ['normalized_numeric_grade', 'normalized_pollscore', 'normalized_transparency_score', 'sample_size_weight', 'state_rank', 'population_weight']

        X = filtered_favorability_df[features_columns].values
        y = filtered_favorability_df['favorable'].values

        if X.shape[0] < min_samples_required:
            print_with_color(f"Not enough data for prediction in {period_value} {period_type} period. Data count: {X.shape[0]}", color_index)
        else:
            # Define the imputation strategy
            imputer = SimpleImputer(strategy='median')

            # Define the pipeline
            pipeline = Pipeline(steps=[
                ('imputer', imputer),
                ('model', RandomForestRegressor(n_estimators=n_trees, oob_score=True, random_state=500, bootstrap=True))
            ])

            # Fit the pipeline
            pipeline.fit(X, y)

            oob_predictions = np.zeros(y.shape)
            for tree in pipeline.named_steps['model'].estimators_:
                unsampled_indices = _get_unsampled_indices(tree, X.shape[0])
                if len(unsampled_indices) > 0:
                    oob_predictions[unsampled_indices] += tree.predict(imputer.transform(X[unsampled_indices]))

            oob_sample_counts = np.array([_get_unsampled_indices(tree, X.shape[0]).size for tree in pipeline.named_steps['model'].estimators_])
            oob_sample_counts = np.bincount(np.concatenate([_get_unsampled_indices(tree, X.shape[0]) for tree in pipeline.named_steps['model'].estimators_]))

            epsilon = np.finfo(float).eps  # Small epsilon value to avoid division by zero
            oob_predictions /= (oob_sample_counts + epsilon)

            oob_variance = np.var(y - oob_predictions)

            if np.isnan(oob_variance):
                print_with_color(f"Insufficient data for reliable OOB variance estimation in {period_value} {period_type} period.", color_index)
            else:
                output_results(combined_results, color_index, period_value, period_type, oob_variance)

        color_index += 1
        
if __name__ == "__main__":
    main()