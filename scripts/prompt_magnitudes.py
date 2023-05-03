# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     custom_cell_magics: kql
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.11.2
#   kernelspec:
#     display_name: AVE
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Exponential growth of residual stream norms

# %%
try:
    import algebraic_value_editing
except ImportError:
    commit = "15bcf55"  # Stable commit
    get_ipython().run_line_magic(  # type: ignore
        magic_name="pip",
        line=(
            "install -U"
            f" git+https://github.com/montemac/algebraic_value_editing.git@{commit}"
        ),
    )


# %%
import torch
import pandas as pd
from typing import List, Dict

from transformer_lens.HookedTransformer import HookedTransformer

from algebraic_value_editing import hook_utils, prompt_utils
from algebraic_value_editing.prompt_utils import RichPrompt


# %%
device: str = "cpu"
model_name = "gpt2-xl"
model: HookedTransformer = HookedTransformer.from_pretrained(model_name, device="cpu")
_ = model.to(device)

_ = torch.set_grad_enabled(False)
torch.manual_seed(0)  # For reproducibility


# %% [markdown]
# Let's examine what the residual stream magnitudes tend to be, by taking the Frobenius
# norm of the residual stream at each sequence position. We'll do this for
# a range of prompts at a range of locations in the forward pass. (The
# downloaded prompts were mostly generated by GPT-4.)

# %%
import requests

url = "https://raw.githubusercontent.com/montemac/algebraic_value_editing/cb6b1a42493a385ca02e7b9e6bbcb9bff9d006dc/scripts/prompts.txt"  # Cached at a commit to prevent changing results

response = requests.get(url)

if response.status_code == 200:
    # If the request is successful, split the content by line breaks to create a list of strings
    prompts = response.text.splitlines()
    print(f"Downloaded {len(prompts)} prompts")
else:
    raise Exception(
        f"Failed to download the file: {response.status_code} -" f" {response.reason}"
    )


# %%
DF_COLS: List[str] = [
    "Prompt",
    "Activation Location",
    "Activation Name",
    "Magnitude",
]

sampling_kwargs: Dict[str, float] = {
    "temperature": 1.0,
    "top_p": 0.3,
    "freq_penalty": 1.0,
}

num_layers: int = model.cfg.n_layers


# %% [markdown]
# ## Residual stream magnitudes increase exponentially with layer number
# As the forward pass progresses through the network, the residual
# stream tends to increase in magnitude in an exponential fashion. This
# is easily visible in the histogram below, which shows the distribution
# of residual stream magnitudes for each layer of the network. The activation
# distribution translates by an almost constant factor each 6 layers,
# and the x-axis (magnitude) is log-scale, so magnitude apparently
# increases exponentially with layer number.*
#
# (Intriguingly, there are a few outlier residual streams which have
# magnitude over an order of magnitude larger than the rest.)
#
# Alex's first guess for the exponential magnitude increase was: Each OV circuit is a linear function of the
# residual stream given a fixed attention pattern. Then you add the head
# OV outputs back into a residual stream, which naively doubles the
# magnitude assuming the OV outputs have similar norm to the input
# residual stream. The huge problem with this explanation is layernorm,
# which is applied to the inputs to the attention and MLP layers. This
# should basically whiten the input to the OV circuits if the gain
# parameters are close to 1.
#
# \* Stefan Heimersheim previously noticed this phenomenon in GPT2-small.

# %%
import plotly.express as px
import plotly.graph_objects as go
import numpy as np


def magnitude_histogram(df: pd.DataFrame) -> go.Figure:
    """Plot a histogram of the residual stream magnitudes for each layer
    of the network."""
    assert (
        "Magnitude" in df.columns
    ), "Dataframe must have a 'Magnitude' column"

    df["LogMagnitude"] = np.log10(df["Magnitude"])

    # Get the number of unique activation locations
    num_unique_activation_locations = df["Activation Location"].nunique()

    # Generate a color list that is long enough to accommodate all unique activation locations
    extended_rainbow = (
        px.colors.sequential.Rainbow * num_unique_activation_locations
    )
    color_list = extended_rainbow[:num_unique_activation_locations]

    fig = px.histogram(
        df,
        x="LogMagnitude",
        color="Activation Location",
        marginal="rug",
        histnorm="percent",
        nbins=100,
        opacity=0.5,
        barmode="overlay",
        color_discrete_sequence=color_list,
    )

    fig.update_layout(
        legend_title_text="Layer Number",
        title="Residual Stream Magnitude by Layer Number",
        xaxis_title="Magnitude (log 10)",
        yaxis_title="Percentage of streams",
    )

    return fig


# %%
# Create an empty dataframe with the required columns
prompt_df = pd.DataFrame(columns=DF_COLS)

from algebraic_value_editing import prompt_utils

# Loop through activation locations and prompts
activation_locations_8: List[int] = torch.arange(
    0, num_layers, num_layers // 8
).tolist()
for act_loc in activation_locations_8:
    act_name: str = prompt_utils.get_block_name(block_num=act_loc)
    for prompt in prompts:
        mags: torch.Tensor = hook_utils.prompt_magnitudes(
            model=model, prompt=prompt, act_name=act_name
        ).cpu()

        # Create a new dataframe row with the current data
        row = pd.DataFrame(
            {
                "Prompt": prompt,
                "Activation Location": act_loc,
                "Activation Name": act_name,
                "Magnitude": mags,
            }
        )

        # Append the new row to the dataframe
        prompt_df = pd.concat([prompt_df, row], ignore_index=True)


# %%
fig: go.Figure = magnitude_histogram(prompt_df)
fig.show()


# %% [markdown]
# In GPT2-XL, the fast magnitude gain
# occurs in the first 7 layers. Let's find out where.

# %%
activation_locations: List[int] = list(range(7))
first_7_df = pd.DataFrame(columns=DF_COLS)

for act_loc in activation_locations:
    prefixes = ["pre", "mid", "post"] if act_loc == 0 else ["mid", "post"]
    for prefix in prefixes:
        act_name = f"blocks.{act_loc}.hook_resid_{prefix}"
        for prompt in prompts:
            mags: torch.Tensor = hook_utils.prompt_magnitudes(
                model=model, prompt=prompt, act_name=act_name
            ).cpu()
            loc_delta = 0 if prefix == "pre" else 0.5 if prefix == "mid" else 1
            # Create a new dataframe row with the current data
            row = pd.DataFrame(
                {
                    "Prompt": prompt,
                    "Activation Location": act_loc + loc_delta,
                    "Activation Name": act_name,
                    "Magnitude": mags,
                }
            )

            # Append the new row to the dataframe
            first_7_df = pd.concat([first_7_df, row], ignore_index=True)

fig: go.Figure = magnitude_histogram(first_7_df)
fig.show()



# %% [markdown]
# Most of the jump happens after the 0th layer in the transformer, and
# a smaller jump happens between the 1st and 2nd layers.

# %% [markdown]
# ## Attention OV matrices and MLPs
#
# Which norm is the correct measure to use? For the residul stream it's just the vector norm of the embedding vector,
# for attn/MLP blocks we want to know: How much does this module increase the norm of its input. A brute-force way to
# test this would be feed-in random inputs and see how much their norm changes. I do this here with randn vectors,
# which technically is not the same distribution but should be fine as we just want to know which matrix norm this
# corresponds to.

# %% [markdown]
# ### Attention OV matrices

# %% [markdown]
# #### Brute-force: feed vectors into OV and see how they change

# %%
from fancy_einsum import einsum

print("Model name:", model_name)

df_OV_scale = pd.DataFrame(columns=["Layer", "Head", "Norm increase"])

for layer in range(model.cfg.n_layers):
    W_OVs = einsum(
        "head hidden embedout, head embed hidden -> head embed embedout",
        model.blocks[layer].attn.W_O,
        model.blocks[layer].attn.W_V,
    )
    random_embed = torch.randn(1000, model.cfg.d_model).to(device)
    random_embed /= random_embed.norm(dim=-1, keepdim=True)
    OV_output = torch.zeros(1000, model.cfg.n_heads + 1, model.cfg.d_model).to(device)
    OV_output[:, : model.cfg.n_heads, :] = einsum(
        "batch embed, head embed embedout -> batch head embedout",
        random_embed,
        W_OVs,
    )
    OV_output[model.cfg.n_heads] = model.blocks[layer].attn.b_O.view(1, 1, -1)
    norm_increase = OV_output.norm(dim=-1)
    total_norm_increase = OV_output.sum(dim=1).norm(dim=-1)
    mean_norm_increase = norm_increase.mean(dim=0)
    mean_total_norm_increase = total_norm_increase.mean(dim=0)
    df_OV_scale = pd.concat(
        [
            df_OV_scale,
            pd.DataFrame(
                [[layer, "Sum", mean_total_norm_increase.item()]],
                columns=["Layer", "Head", "Norm increase"],
            ),
        ],
        ignore_index=True,
    )
    for head in range(model.cfg.n_heads):
        # print(f"Layer {layer:2d} Head {head:2d} OV matrix increases embedding norm by factor {mean_norm_increase[head]:.2f}")
        df_OV_scale = pd.concat(
            [
                df_OV_scale,
                pd.DataFrame(
                    [[layer, head, mean_norm_increase[head].item()]],
                    columns=["Layer", "Head", "Norm increase"],
                ),
            ],
            ignore_index=True,
        )
    df_OV_scale = pd.concat(
        [
            df_OV_scale,
            pd.DataFrame(
                [
                    [
                        layer,
                        "Bias",
                        mean_norm_increase[model.cfg.n_heads].item(),
                    ]
                ],
                columns=["Layer", "Head", "Norm increase"],
            ),
        ],
        ignore_index=True,
    )

# Scatter Layer scale, log scale
fig = px.scatter(
    df_OV_scale,
    x="Layer",
    y="Norm increase",
    color="Head",
    log_y=True,
    title=(
        "How much the W_OV matrices increase the norm of the input by layer" " and head"
    ),
)
fig.show()



# %% [markdown]
# #### Check results identical to the ones from Slack
#
# Last point off because the Slack version forgot biases

# %%
import matplotlib.pyplot as plt

print("Model name:", model_name)
stds = []
norms = []
for layer in range(model.cfg.n_layers):
    OVs = einsum(
        "head hidden embedout, head embed hidden -> head embed embedout",
        model.blocks[layer].attn.W_O,
        model.blocks[layer].attn.W_V,
    )
    random_embed = torch.randn(1000, model.cfg.d_model).to(device)
    random_OVs = einsum(
        "batch embed, head embed embedout -> batch head embedout",
        random_embed,
        OVs,
    )
    std = random_OVs.sum(dim=1).std(dim=-1).mean(dim=0).item()
    norm = random_OVs.sum(dim=1).norm(dim=-1).mean(dim=0).item()
    stds.append(std)
    norms.append(norm)
    print(f"Layer {layer:02d}, random OV output std: {std:.4f}")
plt.plot(stds)
plt.plot(np.array(norms) / np.sqrt(model.cfg.d_model), ls=":")
plt.scatter(
    range(model.cfg.n_layers),
    df_OV_scale[df_OV_scale.Head == "Sum"]["Norm increase"],
    c="r",
    marker="x",
)
plt.xlabel("layer")
plt.ylabel("Std of random OV-output")
plt.title(model_name)



# %% [markdown]
# #### Frobenius norm
#
# `W_OV.norm(dim=(-2,-1))` (used below) is close to the result above and `b_O` is usually negligible. Note: `b_V` is set to zero (folded-in to other weights)

# %%
from fancy_einsum import einsum

print("Model name:", model_name)

df_OV_scale = pd.DataFrame(columns=["Layer", "Head", "Norm increase"])

for layer in range(model.cfg.n_layers):
    assert torch.allclose(
        torch.zeros(1), model.blocks[layer].attn.b_V
    ), "b_V should be zero in default TransformerLens"
    W_OVs = einsum(
        "head hidden embedout, head embed hidden -> head embed embedout",
        model.blocks[layer].attn.W_O,
        model.blocks[layer].attn.W_V,
    )
    mean_norm_increase = W_OVs.norm(dim=(-2, -1)) / np.sqrt(model.cfg.d_model)
    for head in range(model.cfg.n_heads):
        # print(f"Layer {layer:2d} Head {head:2d} OV matrix increases embedding norm by factor {mean_norm_increase[head]:.2f}")
        df_OV_scale = pd.concat(
            [
                df_OV_scale,
                pd.DataFrame(
                    [[layer, head, mean_norm_increase[head].item()]],
                    columns=["Layer", "Head", "Norm increase"],
                ),
            ],
            ignore_index=True,
        )

# Scatter Layer scale, log scale
fig = px.scatter(
    df_OV_scale,
    x="Layer",
    y="Norm increase",
    color="Head",
    log_y=True,
    title="Frobenius norms",
)
fig.show()



# %% [markdown]
# ### MLPs
#
# Main complication is ReLU. Can try to account for average number of dead neurons per layer but obviously biased and does not work.

# %% [markdown]
# #### Brute force test: Just throw randn vectors into mlp()

# %%
from fancy_einsum import einsum

print("Model name:", model_name)

df_MLP_scale = pd.DataFrame(columns=["Layer", "Norm increase", "Source"])
ReLU_zero_rates = {}

for layer in range(model.cfg.n_layers):
    random_embed = torch.randn(1000, 1, model.cfg.d_model).to(device)
    random_embed /= random_embed.norm(dim=-1, keepdim=True)
    mlp_out = model.blocks[layer].mlp(random_embed)
    norm_increase = mlp_out[:, 0, :].norm(dim=-1)
    mean_norm_increase = norm_increase.mean(dim=0)
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, mean_norm_increase.item(), "Real"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )
    hidden = (
        einsum(
            "batch pos embed, embed hidden -> batch pos hidden",
            random_embed,
            model.blocks[layer].mlp.W_in,
        )
        + model.blocks[layer].mlp.b_in
    )
    ReLU_zero_rate = (hidden[:, 0, :] < 0).float().mean()
    ReLU_zero_rates[layer] = ReLU_zero_rate
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, ReLU_zero_rate.item(), "dead-fraction"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )

# Scatter Layer scale, log scale
fig = px.scatter(
    df_MLP_scale,
    x="Layer",
    y="Norm increase",
    color="Source",
    log_y=True,
    title="How much the MLP increase the norm of the input, by layer",
)
fig.show()



# %% [markdown]
# #### Compare to matrices
#
# Calculate the following terms:
# * Norm of W_in * W_out
# * Norm of b_in * W_out
# * Norm of b_out
# * Naive total by summing the three terms, and multiplying the former two with the ReLU dead-rate. This may be inaccurate as the ReLU dead-rate and hidden values are correlated, but what this correlation means to the output is non-trivial to me

# %%
from fancy_einsum import einsum

print("Model name:", model_name)

# Code from above
df_MLP_scale = pd.DataFrame(columns=["Layer", "Norm increase", "Source"])
ReLU_zero_rates = {}

for layer in range(model.cfg.n_layers):
    random_embed = torch.randn(1000, 1, model.cfg.d_model).to(device)
    random_embed /= random_embed.norm(dim=-1, keepdim=True)
    mlp_out = model.blocks[layer].mlp(random_embed)
    norm_increase = mlp_out[:, 0, :].norm(dim=-1)
    mean_norm_increase = norm_increase.mean(dim=0)
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, mean_norm_increase.item(), "Real"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )
    hidden = (
        einsum(
            "batch pos embed, embed hidden -> batch pos hidden",
            random_embed,
            model.blocks[layer].mlp.W_in,
        )
        + model.blocks[layer].mlp.b_in
    )
    ReLU_zero_rate = (hidden[:, 0, :] < 0).float().mean()
    ReLU_zero_rates[layer] = ReLU_zero_rate
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, ReLU_zero_rate.item(), "dead-fraction"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )


# Matrix based calculation
for layer in range(model.cfg.n_layers):
    ReLU_zero_rate = ReLU_zero_rates[layer]
    Winout = einsum(
        "d_model_in d_mlp, d_mlp d_model_out -> d_model_in d_model_out",
        model.blocks[layer].mlp.W_in,
        model.blocks[layer].mlp.W_out,
    )
    Winout_mean_norm_increase = Winout.norm(dim=(-2, -1))
    bin_mean_norm_increase = einsum(
        "d_mlp, d_mlp d_model -> d_model",
        model.blocks[layer].mlp.b_in,
        model.blocks[layer].mlp.W_out,
    ).norm(dim=-1)
    bout_mean_norm_increase = model.blocks[layer].mlp.b_out.norm(dim=-1)
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, Winout_mean_norm_increase.item(), "WinWout"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, bin_mean_norm_increase.item(), "binWout"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [[layer, bout_mean_norm_increase.item(), "bout"]],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )
    df_MLP_scale = pd.concat(
        [
            df_MLP_scale,
            pd.DataFrame(
                [
                    [
                        layer,
                        (
                            ReLU_zero_rate
                            * (Winout_mean_norm_increase + bin_mean_norm_increase)
                            + bout_mean_norm_increase
                        ).item(),
                        "Naive total",
                    ]
                ],
                columns=["Layer", "Norm increase", "Source"],
            ),
        ],
        ignore_index=True,
    )

# Scatter Layer scale, log scale
fig = px.line(
    df_MLP_scale,
    x="Layer",
    y="Norm increase",
    color="Source",
    log_y=True,
    title="MLP components",
)
fig.show()



# %% [markdown]
# ## Plotting residual stream magnitudes against layer number
# Let's zoom in on how specific token magnitudes evolve over a forward
# pass. It turns out that the zeroth position (the `<|endoftext|>` token) has a much larger
# magnitude than the rest. (This possibly explains the outlier
# magnitudes for the prompt histograms.)

# %%
def line_plot(
    df: pd.DataFrame,
    log_y: bool = True,
    title: str = "Residual Stream Norm by Layer Number",
    legend_title_text: str = "Prompt",
) -> go.Figure:
    """Make a line plot of the RichPrompt norm. If log_y is True,
    adds a column to the dataframe with the log10 of the norm."""
    for col in ["Prompt", "Activation Location", "Magnitude"]:
        assert col in df.columns, f"Column {col} not in dataframe"

    if log_y:
        df["LogMagnitude"] = np.log10(df["Magnitude"])

    fig = px.line(
        df,
        x="Activation Location",
        y="LogMagnitude" if log_y else "Magnitude",
        color="Prompt",
        color_discrete_sequence=px.colors.sequential.Rainbow[::-1],
    )

    fig.update_layout(
        legend_title_text=legend_title_text,
        title=title,
        xaxis_title="Layer Number",
        yaxis_title=f"Norm{' (log 10)' if log_y else ''}",
    )

    return fig



# %%
# Create an empty dataframe with the required columns
all_resid_pre_locations: List[int] = torch.arange(0, num_layers, 1).tolist()
addition_df = pd.DataFrame(columns=DF_COLS)

# Loop through activation locations and prompts
for act_loc in all_resid_pre_locations:
    act_name: str = prompt_utils.get_block_name(block_num=act_loc)

    for context in ("MATS is really cool",):
        mags: torch.Tensor = hook_utils.prompt_magnitudes(
            model=model, prompt=context, act_name=act_name
        ).cpu()
        str_tokens: List[str] = model.to_str_tokens(context)

        for pos, mag in enumerate(mags):
            # Create a new dataframe row with the current data
            row = pd.DataFrame(
                {
                    "Prompt": [str_tokens[pos]],
                    "Activation Location": [act_loc],
                    "Activation Name": [act_name],
                    "Magnitude": [mag],
                }
            )

            # Append the new row to the dataframe
            addition_df = pd.concat([addition_df, row], ignore_index=True)


# %%
for use_log in (True, False):
    fig = line_plot(
        addition_df,
        log_y=use_log,
        title=f"Residual Stream Norm by Layer Number in {model_name}",
    )
    fig.update_layout(width=600)
    fig.show()

# %% [markdown]
# To confirm the exponential increase in magnitude, let's plot the
# Frobenius
# norm of the residual stream at position `i` just before layer `t`,
# divided by the norm before `t-1`.

# %%
# Make a plotly line plot of the relative magnitudes vs layer
# number, with color representing the token location of the "MATS is
# really cool" prompt

# Create an empty dataframe with the required columns
all_resid_pre_locations: List[int] = torch.arange(1, num_layers, 1).tolist()
relative_df = pd.DataFrame(columns=DF_COLS)
MATS_prompt: str = "MATS is really cool"

mags_prev: torch.Tensor = hook_utils.prompt_magnitudes(
    model=model, prompt=MATS_prompt, act_name=prompt_utils.get_block_name(0)
).cpu()

# Loop through activation locations and prompts
for act_loc in all_resid_pre_locations:
    act_name: str = prompt_utils.get_block_name(block_num=act_loc)
    mags: torch.Tensor = hook_utils.prompt_magnitudes(
        model=model, prompt=MATS_prompt, act_name=act_name
    ).cpu()

    tokens: List[str] = model.to_str_tokens(MATS_prompt)
    for pos, mag in enumerate(mags):
        # Create a new dataframe row with the current data
        row = pd.DataFrame(
            {
                "Prompt": [tokens[pos]],
                "Activation Location": [act_loc],
                "Activation Name": [act_name],
                "Magnitude": [mag / mags_prev[pos]],
            }
        )

        # Append the new row to the dataframe
        relative_df = pd.concat([relative_df, row], ignore_index=True)

    mags_prev = mags


# %%
relative_fig = line_plot(
    relative_df,
    log_y=False,
    title=f"Norm(n)/Norm(n-1) across layers n in {model_name}",
    legend_title_text="Token",
)

# Set y label to be "Norm growth rate"
relative_fig.update_yaxes(title_text="Norm growth rate")

# Set y bounds to [.9, 1.5]
relative_fig.update_yaxes(range=[0.9, 1.5])

# Plot a horizontal line at y=1
relative_fig.add_hline(y=1, line_dash="dash", line_color="black")
relative_fig.update_layout(width=600)

relative_fig.show()

# %%
# Print the geometric mean of the magnitude growth rates
for pos in range(6):
    pos_df: pd.DataFrame = relative_df[relative_df["Prompt"] == tokens[pos]]
    geom_avg: float = pos_df["Magnitude"].prod() ** (1 / len(pos_df))
    print(
        f"The `{tokens[pos]}` token (position {pos}) has an average growth"
        f" rate of {geom_avg:.3f}"
    )


# %% [markdown]
# The exponential increase in magnitude is confirmed, with tokens having
# an average growth rate of about 1.12. Once again, the `<|endoftext|>` token is an outlier.