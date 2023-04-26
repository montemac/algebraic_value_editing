# %%
# Imports, etc
import pickle
import textwrap

import numpy as np
import pandas as pd
import scipy as sp
import torch
from tqdm.auto import tqdm
from IPython.display import display
import plotly.express as px
import plotly.graph_objects as go
import plotly as py
import plotly.subplots
import nltk
import nltk.data

from transformer_lens import HookedTransformer

from algebraic_value_editing import (
    hook_utils,
    prompt_utils,
    utils,
    completion_utils,
    metrics,
    sweeps,
    experiments,
)

utils.enable_ipython_reload()

# Disable gradients to save memory during inference
_ = torch.set_grad_enabled(False)

py.offline.init_notebook_mode()


# %%
# Load a model
MODEL: HookedTransformer = HookedTransformer.from_pretrained(
    model_name="gpt2-xl", device="cpu"
).to("cuda:1")


# %%[markdown]
# ## Quantitative Evaluations of Activation Injection on a Language Model
#
# Reading model completions in the above qualitative results section is
# an engaging way to build some intuitions for the effects of the
# activation injections. This section compliments this by presenting
# several tools to quantity the *effectiveness* and *focus* of the Activation
# Injection technique. Here *effectiveness* refers to the ability to change model
# behavior in the intended way (i.e. "did it work?") and *focus*
# refers to the ability to preserve model behavior and capabilities that
# are orthogonal or unrelated to the intended change (i.e. "did we avoid
# breaking something else?")
#
# Both effectiveness and focus are properties of the probability
# distribution over next tokens conditioned on an input sequence that is
# implicitly defined by the model. An effective
# intervention would, on average, increase the probabilities of next
# tokens that are consistent with the steering goal, and decrease the
# probability of tokens that are opposed to the goal; a focused
# intervention would minimize changes in probability associated with
# tokens that are orthogonal or unrelated to the steering goal (modulo
# any required distribution re-normalization of course). Mathematically,
# we define these quantities in terms of log-probabilities of
# sets of tokens in the modified model compared with the original model.
#
# Let $P(t,\textbf{s})$ denote the joint distribution over next tokens
# and input sequences implied by the modified model, and
# $Q(t,\textbf{s})$ the same distribution for the original model, where
# $t \in T$ is a next token and $\textbf{s} \in S$ is an input sequence.
# We define effectiveness and focus as:
#
# Effectiveness:
# $$
# \Epsilon =
#   \sum_{\textbf{s} \in S} P(\textbf{s}) \frac{1}{|T_A(\textbf{s})|} \sum_{t \in T_A(\textbf{s})}
#       log \left(\frac{P(t,\textbf{s})}{Q(t,\textbf{s})}\right) -
#   \sum_{\textbf{s} \in S} P(\textbf{s}) \frac{1}{|T_O(\textbf{s})|}\sum_{t \in T_O(\textbf{s})}
#       \frac{1}{|S|} log \left(\frac{P(t,\textbf{s})}{Q(t,\textbf{s})}\right)
# $$
#
# Focus:
# $$
# \Phi =
#   \sum_{\textbf{s} \in S} \sum_{t \in T(\textbf{s})}
#      P(t,\textbf{s}) log \left(\frac{P(t,\textbf{s})}{Q(t,\textbf{s})}\right)
# $$
#
# where $T_A(\textbf{s})$, $T_O(\textbf{s})$
# are the sets of steering-aligned and steering-opposed tokens for a
# given input sequence $\textbf{s}$.
#
# Note that focus is simply defined as the Kullback–Leibler divergence.
#
# These definitions are natural, but not directly useful in practice for
# at least two reasons: (1) the large input space of language models makes
# it impossible to operate on the full space of possible input sequences
# $S$; the best we
# can do is operate some conditional distributions defined by
# paticular inputs; and (2) for most steering goals, the set of tokens
# for which a probability increase or decrease would be considered evidence of
# effectivenes (i.e. the steering-aligned and steering-opposite sets
# $T_A$ and $T_O$) cannot be defined easily, and will depend heavily on the specific
# input text.
#
# Given these constraints, in this section we will use two specific
# techniques to get proxy measures for the idealized effectiveness and
# focus quantities:
#
# 1. "Zoom in" on one specific input sequence and steering goal, which
#    allows us to operate on the actual full next-token distribution and
#    define steering-aligned next-token sets.  This technique is useful
#    for building a low-level token-by-token understanding of the impact
#    of an activation injection on a model's behavior in a specific
#    input case.
# 2. "Zoom out" over a larger corpus of input sequences that are labeled
#    into classes that are relevent to the steering goal. We can then
#    define our effectiveness and focus metrics in terms of "mean loss
#    deltas" (i.e. mean difference in loss of the modified model vs the
#    original model) across the different input classes.  This technique
#    is useful for evaluating the effect of activation injections on the
#    probability assigned to large sets of input sequences to see how
#    well behavior generalizes.  This technique is consistent with the
#    above definitions since mean loss difference equates to mean
#    difference in log probabilities, which is proportional to the
#    summations above.  In effect, using mean loss diffs acros classes
#    is equivalent to assigning all the actual next tokens in the corpus
#    to different steering-related sets based on the label of each
#    sequence, a gross oversimplication but useful nonetheless!
#
# These two techniques will be demonstrated through a simple case study:
# steering the model to be more likely to talk about weddings.
#
# ### Impact on Next-Token Distribution
#
# When we "zoom in" on a single input text, the logits generated on a
# forward pass of the model directly specify the distribution over next
# tokens, which allows us to use the exact definitions for effectiveness
# and focus given above for this specific input sequence.
#
# TODO:
# - Define weddings-related tokens
# - Show single example with actual effectiveness and focus values.
# - Show Uli's viz or something similar
# - Show sweeps of effectiveness and focus over coeffs/layers for this example
#
# ### Impact on Loss Over Relevant Corpus
#
# Turning now to our second technique, we first need to find or generate
# a corpus of sequences that are relevant to our steering goal.  For
# this example, we used the following procedure:
# 1. Give GPT-4 the following prompt: "Please write a 1-2 page summary of
#    recent trends in the wedding industry.  Please try to be as
#    comprehensive as possible."
# 2. Take the result, tokenize into sentences, label these sentences as
#    "wedding-related".
# 3. Repeat with the prompt: "Please write a 1-2 page summary of
#    recent trends in the shipping industry.  Please try to be as
#    comprehensive as possible."
#
# This results in a set of "wedding-related" sentences, and a set of
# "wedding-unrelated" (i.e. "shipping-industry-related") sentences.
#
# We can then perform a forward pass through original and modified
# models for each sentence in the corpus, storing the per-token loss at
# each position $k>=1$. Next, we take the difference in loss between
# the modified and original models, and take the mean over all positions in
# each sequence, with an important modification: the loss at the actual
# injection position(s) is masked from this calculation.  Emprically, we
# have found a large spike in loss at this position, which we feel
# simply adds noise to the results that is not relevant for
# understanding actual model behavior, since in practice the injection
# is always made at positions *before* the first token that would be
# sampled from the model and hence the actual model outputs in very
# early tokens are irrelevant.

# Finally, we group sequences by label, and take the mean of each
# resulting label class.  This results in a dataset containing a mean
# loss delta for each of the classes in the input ()"wedding-related"
# and "wedding-unrelated").  We can repeat this process for any
# combination of activation injection hyperparameters (e.g. coefficient,
# injection layer).  Shown below are the results for a simple activation
# injection x-vector of `[(" weddings", 1.0), ("", -1.0)]` (which is
# internally space-padded to ensure both phrases match in token length),
# swept over coefficient, for a handful of injection layers.
#
# TODO: describe exact setup

# %%
# Perform the weddings experiment
FILENAMES = {
    "weddings": "../data/chatgpt_wedding_essay_20230423.txt",
    "shipping": "../data/chatgpt_shipping_essay_20230423.txt",
}


nltk.download("punkt")
tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")

# Tokenize into sentences
texts = []
for desc, filename in FILENAMES.items():
    with open(filename, "r") as file:
        sentences = [
            "" + sentence for sentence in tokenizer.tokenize(file.read())
        ]
    texts.append(
        pd.DataFrame({"text": sentences, "is_weddings": desc == "weddings"})
    )
texts_df = pd.concat(texts).reset_index(drop=True)

# Perform experiment and show results
USE_CACHE = True
CACHE_FN = "weddings_essays_cache.pkl"
if USE_CACHE:
    with open(CACHE_FN, "rb") as file:
        fig, mod_df, results_grouped_df = pickle.load(file)
else:
    fig, mod_df, results_grouped_df = experiments.run_corpus_loss_experiment(
        corpus_name="weddings/shipping essays",
        model=MODEL,
        labeled_texts=texts_df[["text", "is_weddings"]],
        x_vector_phrases=(" weddings", ""),
        act_names=[0, 6],
        # coeffs=np.linspace(-2, 2, 101),
        coeffs=np.linspace(-2, 2, 21),
        # coeffs=[-1, 0, 1],
        method="mask_injection_loss",
        label_col="is_weddings",
        color_qty="is_weddings",
    )
fig.show()

# %%
# Cache results
# TODO: use logging
with open(CACHE_FN, "wb") as file:
    pickle.dump((fig, mod_df, results_grouped_df), file)


# %%[markdown]
# From this plot, we can make a few obserations:
# - Injecting at layer 6 with coefficient ~= 1.0, we are able to show
#   evidence supporting *effectiveness* (i.e. a relative increase in the
#   probability assigned to the wedding-related texts vs the
#   non-wedding-related) without
#   sacrificing significant *focus* (i.e. by only making relatively
#   small increases to non-wedding-related loss).
# - With increasing absolute value of injection coefficient, average
#   loss across all sequences tends to increase. This is to be expected,
#   as both of these directions are making progressively larger changes
#   to the original model.
# - Layer 6 causes lower loss in general and appears to result in a more
#   effective intervention.  (In this case, injecting in layer 0 is equivalent to
#   directly superimposing the injection phrase tokens and the input
#   text tokens.)
# - Coefficients above 1.0 begin to *increase* loss on the
#   weddings-related sequences.  Our interpretation is that as
#   coefficients increase, weddings-related tokens are becoming more
#   likely, but general capabilities of the model and starting to
#   slowly degrade (i.e. the focus is imperfect), and that this latter
#   effect starts to dominate.
#
# We've also applied this technique to a different dataset of Yelp
# restaurant reviews, labeled by sentiment, with a steering goal of
# increasing the probability of negative reviews.
#
# A summary of the data processing steps in this case:
# 1. Assign sentiment rating to each review, with 4-5: positive, 3:
#    neutral, <3: negative.
# 2. Sample N=? reviews each with positive and negative sentiment.
# 3. Tokenize into sentences, and assign each sentence the sentiment of
#    the review it was taken from.  (We use sentences instead of entire
#    reviews as some reviews are long, which "dilutes" the effect of the
#    initial activation injection. We believe this is a principled
#    decision, because it is well known that the influence of
#    activations at early positions wanes quickly, and thus our ability
#    to influence loss at later positions with a single intervention at
#    the first positions is understandably limited.)
#
# The results of this experiment are shown below.
#
# TODO: describe exact setup

# %%
# # Load restaurant sentiment data and post-process
# yelp_data = pd.read_csv("../data/restaurant.csv")

# # Assign a sentiment class
# yelp_data.loc[yelp_data["stars"] == 3, "sentiment"] = "neutral"
# yelp_data.loc[yelp_data["stars"] < 3, "sentiment"] = "negative"
# yelp_data.loc[yelp_data["stars"] > 3, "sentiment"] = "positive"

# # Exclude non-english reviews
# yelp_data = yelp_data[yelp_data["text"].apply(langdetect.detect) == "en"]

# # Pick the columns of interest
# yelp_data = yelp_data[["stars", "sentiment", "text"]]

# Load pre-processed
yelp_data = pd.read_csv("../data/restaurant_proc.csv").drop(
    "Unnamed: 0", axis="columns"
)

num_each_sentiment = 20
offset = 0
yelp_sample = pd.concat(
    [
        yelp_data[yelp_data["sentiment"] == "positive"].iloc[
            offset : (offset + num_each_sentiment)
        ],
        yelp_data[yelp_data["sentiment"] == "negative"].iloc[
            offset : (offset + num_each_sentiment)
        ],
    ]
).reset_index(drop=True)

nltk.download("punkt")
tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")

yelp_sample_sentences_list = []
for idx, row in yelp_sample.iterrows():
    sentences = tokenizer.tokenize(row["text"])
    yelp_sample_sentences_list.append(
        pd.DataFrame(
            {
                "text": sentences,
                "sentiment": row["sentiment"],
                "review_sample_index": idx,
            }
        )
    )
yelp_sample_sentences = pd.concat(yelp_sample_sentences_list).reset_index(
    drop=True
)
# Filter out super short sentences
MIN_LEN = 6
yelp_sample_sentences = yelp_sample_sentences[
    yelp_sample_sentences["text"].str.len() >= MIN_LEN
]

# Use the experiment function
USE_CACHE = True
CACHE_FN = "yelp_reviews_cache.pkl"
if USE_CACHE:
    with open(CACHE_FN, "rb") as file:
        fig, mod_df, results_grouped_df = pickle.load(file)
else:
    fig, mod_df, results_grouped_df = experiments.run_corpus_loss_experiment(
        corpus_name="Yelp reviews",
        model=MODEL,
        # labeled_texts=yelp_sample[["text", "sentiment"]],
        labeled_texts=yelp_sample_sentences[["text", "sentiment"]],
        x_vector_phrases=(" worst", ""),
        act_names=[0, 6],
        # act_names=[6],
        coeffs=np.linspace(-2, 2, 11),
        # coeffs=[-1, 0, 1],
        # coeffs=[0],
        method="mask_injection_loss",
        # method="normal",
        # facet_col_qty=None,
        label_col="sentiment",
        color_qty="sentiment",
    )
fig.show()

# %%
# Cache results
# TODO: use logging
with open(CACHE_FN, "wb") as file:
    pickle.dump((fig, mod_df, results_grouped_df), file)


# %%[markdown]
# From this plot, we can make a few obserations:
# - TODO

# %%
#
#
#
#
#
#

# %%[markdown]
# ### Steering Goal: "talk about weddings a lot"
#
# Here we'll use a simple topic-related steering goal to demonstrate the
# evaluation tools and learn a bit about how effectiveness and
# specificity respond to different hyperparameters.  First, let's define
# some constants and helpers:

# %%
# Define prompts, etc.

# Prompts to test
SINGLE_PROMPT = ["Frozen starts off with a scene about"]
PROMPTS = [
    "I went up to my friend and said",
    "Frozen starts off with a scene about",
]

# Phrases to use as the patch input
RICH_PROMPT_PHRASES = [
    [
        (" weddings", 1.0),
        (" ", -1.0),
    ]
]

# The wedding-words-count metric
# TODO: add more metrics
METRICS_DICT = {
    "wedding_words": metrics.get_word_count_metric(
        [
            "wedding",
            "weddings",
            "wed",
            "marry",
            "married",
            "marriage",
            "bride",
            "groom",
            "honeymoon",
        ]
    ),
}

# Coefficients and layers to sweep over in the "layers-dense" sweep
ACT_NAMES_SWEEP_COEFFS = [-1, 1, 2, 4]
ACT_NAMES_SWEEP_ACT_NAMES = [
    prompt_utils.get_block_name(block_num=num)
    for num in range(0, len(MODEL.blocks), 1)
]

# Coefficients and layers to sweep over in the "coefficient-dense" sweep
COEFFS_SWEEP_COEFFS = np.linspace(0, 2, 50)
COEFFS_SWEEP_ACT_NAMES = [
    prompt_utils.get_block_name(block_num=num) for num in [0, 6, 16]
]

# Sampling parameters
SAMPLING_ARGS = dict(seed=0, temperature=1, freq_penalty=1, top_p=0.3)
NUM_NORMAL_COMPLETIONS = 100
NUM_PATCHED_COMPLETIONS = 100
TOKENS_TO_GENERATE = 40


# %%[markdown]
# Next, we'll pick a single prompt ("Frozen starts off with a scene
# about the wedding"), and apply our distribution-based effectiveness and
# specificity evaluations to understand how a weddings-steering
# intervention affects the model's predictions of the final "wedding"
# token.
#
# TODO: show next-tokens prob vizualition
# TODO: show change in probability of "wedding" token and KL divergence
# over coeffs and layers

# %%[markdown]
#
# Having seen that the weddings-steering injection appears to be
# effective and specific in the case of this single example prompt,
# we'll now extend this to a larger corpus of prompts. To generate this
# corpus, we asked GPT-4 to write two short essays, with the following
# prompts:
#
# 1. Please write a 1-2 page summary of recent trends in the shipping
#    industry.  Please try to be as comprehensive as possible.
# 2. Please write a 1-2 page summary of recent trends in the wedding
#    industry.  Please try to be as comprehensive as possible.
#
# Each of the resulting essays is tokenized into sentence, with the
# resulting (labelled) sentences becoming the corpus.
#
# TODO: do the tokenizing here
# TODO: run all the prompts through both models, storing the logits. At
# each position, find the prob increase for the set of wedding-related
# tokens, etc.  Or maybe just use difference in average loss vs the
# unmodified model?  Does this give us everything?  Average increase in
# loss for the full dataset is specificity, difference between classes
# is effectiveness?

# %%[markdown]
# --------------------------------------------------------------------------------
# ## Archive / Drafting

# %%[markdown]
# ## Quantitative Evaluations of Activation Injection on a Language Model
#
# In this section we describe a handful of quantitative evaluations
# intended to assess the *effectiveness* and *specificity* of the Activation
# Injection technique.  Here effectiveness refers to an ability to change model
# behavior in the intended way (i.e. "did it work?") and specificity
# refers to an ability to preserve model behavior and capabilities that
# are orthogonal or unrelated to the intended change (i.e. "did we avoid
# breaking something else?")
#
# We use these tools to "zoom out" and evaluate the technique over a
# range of layers, coefficients, prompts, etc. to identify patterns that
# could help understand or improve the technique. We also use similar tools to
# "zoom in" and build intuitions about how the technique works in detail
# for a few specific examples.
#
# ### Summary of Quantitative Evaluations
#
# We developed several approaches for quantative evaluations which can
# be broken down according to the data evaluated (sampled completions or
# output logits), the disiderata evaluated (effectiveness or
# specificity) and the evaluation method:
# - Completions:
#   - Effectiveness:
#       - Simple heuristics e.g. count topic-related words. Simple, fast
#         and clear, but only suitable for certain steering goals.
#       - Human ratings e.g. "rate out of 10 the happiness of this
#         text". Can evaluate nuanced steering goals, but is slow,
#         scale-limited and hard to calibrate between raters.
#       - ChatGPT ratings using similar prompts. Can evaluate somewhat
#         nuanced goals, is fast and scalable, but also has calibration
#         problems.
#   - Specificity:
#       - Loss on unmodified model. If an injection has "broken the
#         model", we'd expect completions sampled from this model to
#         have much higher loss than a control group of completions of
#         the same prompt on the original model. A challenge for this
#         metric is that a successful steering will of course result in
#         completions that are less probable for the original model, and
#         thus higher loss, even when the technique is "working". One
#         mitigation for this is to use e.g. the median per-token loss
#         rather than the mean, or otherwise remove outliers.  The
#         intuition behind this being that a capable steered model
#         should generate completions that are grammatically correct and
#         sensible despite having a less probable subject, style,
#         sentiment, etc. The "critical tokens" in a given completion whose probability is
#         significantly altered by a successful steerin are likely few
#         in number, with most tokens being primarily determined by
#         grammatical constraints or already-introduced "critical
#         tokens".  Thus, if we take the median loss, we should filter
#         out the affect of these "critical tokens" and better evaluate
#         retained capabilities.
#       - Human ratings as above, but evaluating "coherence" or similar.
#       - ChatGPT ratings as above, but evaluating "coherence" or
#         similar.
# - Logits:
#   - Effectiveness: change in probability of key token(s) at key positions in a
#     specific text sequence. This is the most "zoomed in" metric:
#     looking at a single position in a single sequence, for a small
#     number of possible tokens, and thus provides the most direct
#     and granular visibility into the effect of an injection.
#   - Specificity: KL divergence of the token distributions at key
#     positions in a specific text sequence. A "highly specific"
#     intervention would be expected to change probabilities for a small
#     number of relevant tokens, while leaving the rest of the
#     distribution relatively unchanged.
#
# We going to use an example context to introduce all of these approaches:
# a simple topic-based injection intended to steer the model towards sequences
# that are related to weddings.  After that we'll show results for a
# handful of other steering objectives.
#
# Let's begin with the usual imports and boilerplate, and also load a model:

# %%
# Imports, etc
import pickle
import textwrap

import numpy as np
import pandas as pd
import scipy as sp
import torch
from tqdm.auto import tqdm
from IPython.display import display
import plotly.express as px
import plotly.graph_objects as go
import plotly as py
import plotly.subplots

from transformer_lens import HookedTransformer

from algebraic_value_editing import (
    hook_utils,
    prompt_utils,
    utils,
    sweeps,
    metrics,
    logging,
)

utils.enable_ipython_reload()

# Disable gradients to save memory during inference
_ = torch.set_grad_enabled(False)


# %%
# Load a model
MODEL: HookedTransformer = HookedTransformer.from_pretrained(
    model_name="gpt2-xl", device="cpu"
).to("cuda:1")


# %%[markdown]
# Next, we define some parameters that will be used in this
# wedding-topic-steering example, including the metrics. Specifically:
# - The prompts that will be used to generate completions on the normal
#   and modified models.
# - The phrases, coefficients and injection layers that will be used to
#   intervene on the model.
# - The "relevant words" used in a simple word-count completion
#   effectiveness metric.
# - The prompts used to obtain human and ChatGPT effectiveness and
#   specificiy ratings.

# %%
# Define prompts, etc.

# Prompts to test
PROMPTS = [
    "I went up to my friend and said",
    "Frozen starts off with a scene about",
]

# Phrases to use as the patch input
RICH_PROMPT_PHRASES = [
    [
        (" weddings", 1.0),
        (" ", -1.0),
    ]
]

# The wedding-words-count metric
# TODO: add more metrics
METRICS_DICT = {
    "wedding_words": metrics.get_word_count_metric(
        [
            "wedding",
            "weddings",
            "wed",
            "marry",
            "married",
            "marriage",
            "bride",
            "groom",
            "honeymoon",
        ]
    ),
}

# Coefficients and layers to sweep over in the "layers-dense" sweep
ACT_NAMES_SWEEP_COEFFS = [-1, 1, 2, 4]
ACT_NAMES_SWEEP_ACT_NAMES = [
    prompt_utils.get_block_name(block_num=num)
    for num in range(0, len(MODEL.blocks), 1)
]

# Coefficients and layers to sweep over in the "coefficient-dense" sweep
COEFFS_SWEEP_COEFFS = np.linspace(0, 2, 50)
COEFFS_SWEEP_ACT_NAMES = [
    prompt_utils.get_block_name(block_num=num) for num in [0, 6, 16]
]

# Sampling parameters
SAMPLING_ARGS = dict(seed=0, temperature=1, freq_penalty=1, top_p=0.3)
NUM_NORMAL_COMPLETIONS = 100
NUM_PATCHED_COMPLETIONS = 100
TOKENS_TO_GENERATE = 40

# %%[markdown]
#
# We'll also define a convenience wrapper function to perform a sweep
# with all the required parameters that will be used several times.


# %%
def run_sweep(
    model,
    prompts,
    phrases,
    act_names,
    coeffs,
    metrics_dict,
    log,
    run_path=None,
):
    """Convenience wrapper for performing sweeps, including optional pulling of
    cached data from wandb."""
    # Always generate the RichPrompts df
    rich_prompts_df = sweeps.make_rich_prompts(
        phrases=phrases,
        act_names=act_names,
        coeffs=coeffs,
    )

    # Pull data from a cached run, if path is provided
    if run_path is not None:
        normal_df, patched_df = logging.get_objects_from_run(
            run_path, flatten=True
        )
    # Otherwise perform the run and save the results
    else:
        normal_df, patched_df = sweeps.sweep_over_prompts(
            model=model,
            prompts=prompts,
            rich_prompts=rich_prompts_df["rich_prompts"],
            num_normal_completions=NUM_NORMAL_COMPLETIONS,
            num_patched_completions=NUM_PATCHED_COMPLETIONS,
            tokens_to_generate=TOKENS_TO_GENERATE,
            metrics_dict=metrics_dict,
            log={"tags": ["initial_post"]},
            **SAMPLING_ARGS
        )
        print(logging.last_run_info)

    # Reduce data
    reduced_normal_df, reduced_patched_df = sweeps.reduce_sweep_results(
        normal_df, patched_df, rich_prompts_df
    )

    # Plot
    sweeps.plot_sweep_results(
        reduced_patched_df,
        "wedding_words_count",
        "Average wedding word count",
        col_x="act_name",
        col_color="coeff",
        baseline_data=reduced_normal_df,
    ).show()
    sweeps.plot_sweep_results(
        reduced_patched_df,
        "loss",
        "Average loss",
        col_x="act_name",
        col_color="coeff",
        baseline_data=reduced_normal_df,
    ).show()

    px.scatter(
        reduced_patched_df,
        x="wedding_words_count",
        y="loss",
        color=[int(ss.split(".")[1]) for ss in reduced_patched_df["act_name"]],
        size=reduced_patched_df["coeff"] - reduced_patched_df["coeff"].min(),
        size_max=10,
        facet_col="prompts",
        hover_data=["act_name", "coeff"],
    ).show()

    # Return data for any future use
    return reduced_normal_df, reduced_patched_df


# %%[markdown]
# With these preliminaries in place, we're ready to perform our first
# quantitative evaluation of the weddings-steering intervention. The
# question we're asking here is "how does the effectiveness and
# specificity of the weddings steering change over injection layer for a
# handful of coefficient values?"  Let's find out:

# %%
# Perform a layers-dense sweep and visualize
rich_prompts_df = sweeps.make_rich_prompts(
    phrases=RICH_PROMPT_PHRASES,
    act_names=ACT_NAMES_SWEEP_ACT_NAMES,
    coeffs=ACT_NAMES_SWEEP_COEFFS,
)

# Code to run the actual sweep
# normal_df, patched_df = sweeps.sweep_over_prompts(
#     model=MODEL,
#     prompts=PROMPTS,
#     rich_prompts=rich_prompts_df["rich_prompts"],
#     num_normal_completions=NUM_NORMAL_COMPLETIONS,
#     num_patched_completions=NUM_PATCHED_COMPLETIONS,
#     tokens_to_generate=TOKENS_TO_GENERATE,
#     metrics_dict=METRICS_DICT,
#     log={"tags": ["initial_post"], "group": "wedding-act-names-sweep"},
#     **SAMPLING_ARGS
# )

# Instead, load pre-cached from wandb
normal_df, patched_df = logging.get_objects_from_run(
    "montemac/algebraic_value_editing/6zwyi2au", flatten=True
)

# Before Weights and Biases logging we just cached results locally
# CACHE_FN = "wedding-act-names-sweep.pkl"
# try:
#     with open(CACHE_FN, "rb") as file:
#         normal_df, patched_df, rich_prompts_df = pickle.load(file)
# except FileNotFoundError:
#     normal_df, patched_df = sweeps.sweep_over_prompts(
#         model=MODEL,
#         prompts=PROMPTS,
#         rich_prompts=rich_prompts_df["rich_prompts"],
#         num_normal_completions=NUM_NORMAL_COMPLETIONS,
#         num_patched_completions=NUM_PATCHED_COMPLETIONS,
#         tokens_to_generate=TOKENS_TO_GENERATE,
#         metrics_dict=METRICS_DICT,
#         log={"tags": ["initial_post"], "group": "wedding-act-names-sweep"},
#         **SAMPLING_ARGS
#     )
#     print(logging.last_run_info)
#     with open(CACHE_FN, "wb") as file:
#         pickle.dump((normal_df, patched_df, rich_prompts_df), file)

# Reduce data
reduced_normal_df, reduced_patched_df = sweeps.reduce_sweep_results(
    normal_df, patched_df, rich_prompts_df
)

# Plot
sweeps.plot_sweep_results(
    reduced_patched_df,
    "wedding_words_count",
    "Average wedding word count",
    col_x="act_name",
    col_color="coeff",
    baseline_data=reduced_normal_df,
).show()
sweeps.plot_sweep_results(
    reduced_patched_df,
    "loss",
    "Average loss",
    col_x="act_name",
    col_color="coeff",
    baseline_data=reduced_normal_df,
).show()


px.scatter(
    reduced_patched_df,
    x="wedding_words_count",
    y="loss",
    color=[int(ss.split(".")[1]) for ss in reduced_patched_df["act_name"]],
    size=reduced_patched_df["coeff"] - reduced_patched_df["coeff"].min(),
    size_max=10,
    facet_col="prompts",
    hover_data=["act_name", "coeff"],
).show()

# %%[markdown]
# Some observations jump out immediately.  In no particular order...
#
# **Coefficients beyond 1.0 don't seem to reliably increase
# *effectiveness*, though they do seem to reliably increase loss**
# (TODO: check this with more metrics added.)
#
# (Discuss/analyze)
#
# **Effectiveness broadly decreases as the block number increases,
# though these curves are not simple or linear; loss also tends to
# descrease, but not monotonically.**
#
# At least for the studied prompts, it seems possible to get good
# effectiveness with negligible increase in loss by operating on layer 6
# or 7.
#
# **Negative coefficients have little effect most of the time.**  This
# is not true in general, and appears to be a property of the "negative"
# prompt being a simple space-pad rather than a token with semantic value.
#
# **Why are the patched completions showing lower loss on the original model
# in later layers for the "I went up to my friend and said" prompt,
# while still being effective?**
#
# E.g. block 8, coeff 1?  Maybe changing
# the distribution in such a way that with our sampling approach (top-P,
# temperature, etc.) we're more likely to generate low-loss completions
# or something?  Don't wory about it for now, interesting mystery...

# %%
rpi = rich_prompts_df[
    (rich_prompts_df["coeff"] == 1)
    & (rich_prompts_df["act_name"] == "blocks.8.hook_resid_pre")
].index[0]
prompt = "I went up to my friend and said"
# px.line(patched_df[patched_df["rich_prompt_index"] == rpi]['completion_index'].values)
# display(
#     patched_df[
#         (patched_df["rich_prompt_index"] == rpi)
#         & (patched_df["prompts"] == prompt)
#     ]
# )
# display(normal_df[normal_df["prompts"] == prompt])

# %%[markdown]
# We can perform a similar sweep but with high density on the coefficient
# axis, for a sampling of interesting injection layers (blocks 0, 6,
# 16).

# %%
# Perform a coeffs-dense sweep and visualize
# TODO: wrap all this in a single wrapper function
rich_prompts_df = sweeps.make_rich_prompts(
    phrases=RICH_PROMPT_PHRASES,
    act_names=COEFFS_SWEEP_ACT_NAMES,
    coeffs=COEFFS_SWEEP_COEFFS,
)

# # Code to run the actual sweep
# normal_df, patched_df = sweeps.sweep_over_prompts(
#     model=MODEL,
#     prompts=PROMPTS,
#     rich_prompts=rich_prompts_df["rich_prompts"],
#     num_normal_completions=NUM_NORMAL_COMPLETIONS,
#     num_patched_completions=NUM_PATCHED_COMPLETIONS,
#     tokens_to_generate=TOKENS_TO_GENERATE,
#     metrics_dict=METRICS_DICT,
#     log={"tags": ["initial_post"], "group": "wedding-coeffs-sweep"},
#     **SAMPLING_ARGS
# )

# Instead, load pre-cached from wandb
normal_df, patched_df = logging.get_objects_from_run(
    "montemac/algebraic_value_editing/j7b5c4bb", flatten=True
)

# Reduce data
reduced_normal_df, reduced_patched_df = sweeps.reduce_sweep_results(
    normal_df, patched_df, rich_prompts_df
)

# Plot
sweeps.plot_sweep_results(
    reduced_patched_df,
    "wedding_words_count",
    "Average wedding word count",
    col_x="coeff",
    col_color="act_name",
    baseline_data=reduced_normal_df,
).show()
sweeps.plot_sweep_results(
    reduced_patched_df,
    "loss",
    "Average loss",
    col_x="coeff",
    col_color="act_name",
    baseline_data=reduced_normal_df,
).show()


# %%[markdown]
# Now, let's repeat this basic process for some other RicHPrompts and
# prompts

# %%
# Do a bunch of other sweeps...
# TODO: post-process to add ChatGPT and human-rated metrics


# %%
# Scratchpad ---------------------------

# Generage some wedding-related sentences using ChatGPT
# completion = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=[{"role": "user", "content": "Please generate five sentences that end in the word wedding"}])
# prompts = ["  "+line.split('. ')[1] for line in completion.choices[0].message.content.split('\n')]
prompts = [
    "  The bride wore a stunning white dress with a long flowing train.",
    "  The groom's family surprised everyone with a choreographed dance routine during the reception.",
    "  The wedding was held at a beautiful seaside location, and guests enjoyed breathtaking views of the ocean.",
    "  The couple exchanged personalized vows that brought tears to everyone's eyes.",
    "  The wedding cake was a towering masterpiece, adorned with intricate sugar flowers and delicate piping.",
]


# Convenience function to run a big batch of prompts in parallel, then
# separate them out and return logits and per-token loss objects of the
# original token length of each string.  Returned objects are numpy
# arrays for later analysis convenience
def run_forward_batch(MODEL, prompts):
    logits, loss = MODEL.forward(
        prompts, return_type="both", loss_per_token=True
    )
    logits_list = []
    loss_list = []
    for idx, prompt in enumerate(prompts):
        token_len = MODEL.to_tokens(prompt).shape[1]
        logits_list.append(logits[idx, :token_len, :].detach().cpu().numpy())
        loss_list.append(loss[idx, :token_len].detach().cpu().numpy())
    return logits_list, loss_list


# Run the prompts through the model as a single batch
logits_normal, loss_normal = run_forward_batch(MODEL, prompts)

# Define the activation injection, get the hook functions
rich_prompts = list(
    prompt_utils.get_x_vector(
        prompt1=" weddings",
        prompt2="",
        coeff=1.0,
        act_name=6,
        model=MODEL,
        pad_method="tokens_right",
        custom_pad_id=MODEL.to_single_token(" "),
    ),
)
hook_fns = hook_utils.hook_fns_from_rich_prompts(
    model=MODEL,
    rich_prompts=rich_prompts,
)

# Attach hooks, run another forward pass, remove hooks
MODEL.remove_all_hook_fns()
for act_name, hook_fn in hook_fns.items():
    MODEL.add_hook(act_name, hook_fn)
logits_mod, loss_mod = run_forward_batch(MODEL, prompts)
MODEL.remove_all_hook_fns()


# Plot some stuff
def plot_ind(ind):
    df = pd.concat(
        [
            pd.DataFrame({"loss": loss_normal[ind], "model": "normal"}),
            pd.DataFrame({"loss": loss_mod[ind], "model": "modified"}),
            pd.DataFrame(
                {
                    "loss": loss_mod[ind] - loss_normal[ind],
                    "model": "modified-normal",
                }
            ),
        ]
    )
    fig = px.line(
        df,
        y="loss",
        color="model",
        title=prompts[ind],
    )
    fig.update_layout(
        xaxis=dict(
            tickmode="array",
            tickvals=np.arange(len(MODEL.to_str_tokens(prompts[ind])[1:])),
            ticktext=MODEL.to_str_tokens(prompts[ind])[1:],
        )
    )
    fig.show()


plot_ind(2)

for loss_n, loss_m in zip(loss_normal, loss_mod):
    print(loss_n.mean(), loss_m.mean())
