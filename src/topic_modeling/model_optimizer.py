from collections import Counter
from copy import deepcopy
import os
from typing import Dict, List, Optional, Tuple, Union
import warnings

import openai
import numpy as np
import pandas as pd
from gensim.corpora.dictionary import Dictionary
from gensim.models import CoherenceModel
from tqdm import tqdm
from umap import UMAP
from pyLDAvis import prepared_data_to_html

from plots.topics import interactive_exploration
from topic_modeling.utils import (
    get_filtered_lemmas,
    get_lemmas_dictionary,
    tsne_dim_reduction,
    umap_dim_reduction,
)
from .model import Model


class ModelOptimizer:
    def __init__(
        self,
        df: pd.DataFrame,
        column_filter: Dict[str, str],
        model_type: str = "lda",
        words_to_remove: List[str] = [],
        topic_numbers_range: Tuple[int, int] = (2, 11),
        coherence_measure: str = "c_v",
        coherence_num_words: int = 20, # number of words used for calculating coherence measure
        random_state: Optional[int] = None,
        **kwargs
    ):  
        self.model_type = model_type
        self.column_filter = column_filter
        self.random_state = random_state
        self.coherence_measure = coherence_measure
        self.coherence_num_words = coherence_num_words
        self.data = df.loc[(df[list(column_filter)] == pd.Series(column_filter)).all(axis=1)]
        self.filtered_lemmas = get_filtered_lemmas(self.data, words_to_remove)
        self.lemmas_dictionary = get_lemmas_dictionary(self.filtered_lemmas)
        self.encoded_docs = self.filtered_lemmas.apply(self.lemmas_dictionary.doc2bow)
        self.models = get_models(
            self.encoded_docs, self.model_type, topic_numbers_range, random_state, **kwargs
        )
        self.cvs = get_coherences(self.models, self.filtered_lemmas, self.lemmas_dictionary, self.coherence_measure, self.coherence_num_words)
        self.topics_num = get_best_topics_num(self.cvs)
        self.topic_names_dict = {i:i for i in range(self.topics_num)}

    @property
    def best_model(self):
        return self.models[self.topics_num]

    def get_topics_df(self, num_words: int = 10) -> pd.DataFrame:
        topics = self.best_model.get_topics(num_words=num_words)
        counter = Counter(self.filtered_lemmas.sum())
        id2word_dict = self.lemmas_dictionary
        out = [[
            id2word_dict[int(word_id)],
            topic_id,
            weight,
            counter[id2word_dict[int(word_id)]]
        ]
        for topic_id, topic in topics for word_id, weight in topic]
        df = pd.DataFrame(out, columns=["word", "topic_id", "importance", "word_count"])
        df = df.sort_values(by=["importance"], ascending=False)
        return df

    def get_topic_probs_df(self) -> pd.DataFrame:
        """Returns original data frame with added columns for topic probabilites."""
        corpus_model = self.best_model.get_topic_probs(self.encoded_docs)
        res_len = len(self.data)
        res = np.zeros((res_len, self.topics_num))
        for i, doc in enumerate(corpus_model):
            for topic in doc:
                res[i][topic[0]] = np.round(topic[1], 4)
        modeling_results = pd.concat([self.data.reset_index(drop=True), pd.DataFrame(res)], axis=1)

        return modeling_results

    def get_topic_probs_averaged_over_column(
            self,
            column: str = "country",
            show_names: bool = False,
    ) -> pd.DataFrame:
        """Returns topic probabilities averaged over given column."""
        modeling_results = self.get_topic_probs_df()
        result = []
        column_vals_added = []
        column_vals = modeling_results[column].unique()
        rows_by_column = modeling_results.groupby(column).count()[0].max()
        for column_val in column_vals:
            df_tmp = modeling_results[modeling_results[column] == column_val]
            if df_tmp.shape[0] != rows_by_column:
                warnings.warn(f"{column} - {column_val} has missing rows!")
                continue
            result.append(df_tmp.iloc[:, -self.topics_num :].values.flatten())
            column_vals_added.append(column_val)
        res = pd.DataFrame(np.vstack(result), index=column_vals_added)
        res.index.name = column
        if show_names:
            res.columns = [self.topic_names_dict[i] for i in range(self.topics_num)]
        return res

    def get_tsne_mapping(
        self,
        column: str = "country",
        perplexity: int = 40,
        n_iter: int = 1000,
        init: str = "pca",
        learning_rate: Union[str, float] = "auto",
    ):
        topics_by_country = self.get_topic_probs_averaged_over_column(column)
        mapping = tsne_dim_reduction(
            topics_by_country, self.random_state, perplexity, n_iter, init, learning_rate
        )
        return mapping

    def get_umap_mapping(
        self,
        column: str = "country",
        n_neighbors: int = 7,
        metric: str = "euclidean",
        min_dist: float = 0.1,
        learning_rate: float = 1,
    ):
        topics_by_country = self.get_topic_probs_averaged_over_column(column)
        mapping = umap_dim_reduction(
            topics_by_country,
            self.random_state,
            n_neighbors,
            metric,
            min_dist,
            learning_rate,
        )
        return mapping

    def save(self, path: str = "", label: str = ""):
        filter_name = "_".join([value.replace(" ", "_") for value in self.column_filter.values()])
        self.encoded_docs.to_csv(path + label + "_" + filter_name + "_encoded_docs.csv")
        self.lemmas_dictionary.save(path + label + "_" + filter_name + "_dictionary.dict")
        self.best_model.save(path + label + "_" + filter_name + "_model.model")

    def name_topics_automatically_gpt3(
        self,
        num_keywords: int = 15,
        gpt3_model: str = "text-davinci-003",
        temperature: int = 0.5,
    ) -> pd.DataFrame:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        topics_keywords = self.get_topics_df(num_keywords)
        exculded = []
        for i in range(self.topics_num):
            keywords = topics_keywords[topics_keywords["topic_id"] == i].word.to_list()
            weights = topics_keywords[topics_keywords["topic_id"] == i].importance.to_list()               
            prompt = _generate_prompt(keywords, weights, exculded)
            title = _generate_title(prompt, gpt3_model, temperature)
            self.topic_names_dict[i] = title
            exculded.append(title)


    def name_topics_manually(
        self, topic_names: Union[List[str], Dict[int, str]]
    ) -> pd.DataFrame:
        if isinstance(topic_names, list):
            dict_update = {i:topic_names[i] for i in range(len(topic_names))}
        if isinstance(topic_names, dict):
            dict_update = topic_names
        updated_dict = deepcopy(self.topic_names_dict)
        updated_dict.update(dict_update)
        if updated_dict.keys() == self.topic_names_dict.keys():
            self.topic_names_dict = updated_dict
        else:
            warnings.warn("Topic names not updated: incorrect topic names given")


def save_data_for_app(
    model_optimizer: ModelOptimizer,
    num_words: int = 10,
    column: str = "country",
    perplexity: int = 10,
    n_iter: int = 1000,
    init: str = "pca",
    learning_rate_tsne: Union[str, float] = "auto",
    n_neighbors: int = 7,
    metric: str = "euclidean",
    min_dist: float = 0.1,
    learning_rate_umap: float = 1,
    path: str = "",
    label: str = "",
):
    filter_name = "_".join([value.replace(" ", "_") for value in model_optimizer.column_filter.values()])
    topic_words = model_optimizer.get_topics_df(num_words)
    topics_by_country = model_optimizer.get_topic_probs_averaged_over_column(column)
    model_optimizer.save(path=path, label=label)
    topic_words.to_csv(path + label + "_" + filter_name + "_topic_words.csv")
    topics_by_country.to_csv(path + label + "_" + filter_name + "_probs.csv")
    tsne_mapping = model_optimizer.get_tsne_mapping(
        column,
        perplexity,
        n_iter,
        init,
        learning_rate_tsne,
    )
    umap_mapping = model_optimizer.get_umap_mapping(
        column,
        n_neighbors,
        metric,
        min_dist,
        learning_rate_umap,
    )
    mappings = tsne_mapping.join(umap_mapping)
    mappings.to_csv(path + label + "_" + filter_name + "_mapping.csv")


def get_best_topics_num(cvs: Dict[int, float]) -> int:
    return max(cvs, key=cvs.get)


def get_models(
    corpus: Union[pd.Series, List[List[str]]],
    model_type: str = "lda",
    topic_numbers_range: Tuple[int, int] = (2, 11),
    random_state: Optional[int] = None,
    **kwargs,
) -> Dict[int, Model]:
    return {
        num_topics: Model(
            num_topics=num_topics,
            corpus=corpus, 
            model_type=model_type,
            random_state=random_state,
            **kwargs
        )
        for num_topics in tqdm(range(*topic_numbers_range))
    }


def get_coherences(
    models: Dict[int, Model],
    texts: Union[pd.Series, List[List[str]]],
    dictionary: Dictionary,
    coherence: str = "c_v",
    num_words: int = 20,
) -> Dict[int, float]:
    return {
        num_topics: CoherenceModel(
            topics = model.get_topics_list(dictionary=dictionary, num_words=num_words), 
            texts=texts, 
            dictionary=dictionary, 
            coherence=coherence
        ).get_coherence()
        for num_topics, model in tqdm(models.items())
    }


def _generate_prompt(keywords: list, weights: list, excluded: list = []) -> str:
    keywords_weights = [word + ": " + str(weight) for word, weight in zip(keywords, weights)]
    if len(excluded) > 0:
        excluded_str = f"different than: {', '.join(excluded)} "
    else:
        excluded_str = ""
    return (
        f"Generate short (maximum three words) title {excluded_str}based on given keywords and their importance: "
        + ", ".join(keywords_weights)
    )


def _generate_title(prompt: str, gpt3_model: str, temperature: int) -> str:
    response = openai.Completion.create(model=gpt3_model, prompt=prompt, temperature=temperature)
    return response.choices[0].text.split("\n")[-1].replace('"', '')