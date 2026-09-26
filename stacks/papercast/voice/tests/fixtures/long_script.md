# Flow matching, from the ground up

Today's paper is about a way of training generative models that has quietly taken over a large part of the field. It is called flow matching, and the paper we are looking at is the one that gave it that name. If you have spent time with diffusion models, a lot of this will feel familiar, and that is the point. The authors take the machinery of diffusion, strip away the parts that were only there for historical reasons, and keep the part that actually does the work. What is left is simpler to explain, simpler to train, and, as the results show, often better.

Let me start with the problem, because the whole method is an answer to one question. You have a pile of examples, say pictures, or crystal structures, or molecules, and you want a machine that can produce new ones that look like they came from the same pile. The standard trick is to start from something easy, plain random noise, and learn a way to turn that noise into data. Everyone agrees on that much. The disagreement is about how you describe the turning, and how you teach a network to do it.

# The picture to hold in your head

Imagine every possible picture as a point in an enormous space. The noise you start from is a cloud of points spread evenly in every direction, like fog. Your real data is a much stranger shape: thin sheets and filaments where the actual pictures live, with vast empty regions in between. A generative model is a way of moving the fog so that it ends up lying exactly on those sheets.

Flow matching describes that movement as a velocity field. At every point in the space, and at every moment in time between zero and one, the field says which direction to move and how fast. If you drop a particle into the fog at time zero and let it follow the arrows, by time one it should land on the data. Do that for every particle in the fog, and the whole cloud has been carried over onto the shape of the data. The network's only job is to predict that velocity: give it a point and a time, and it tells you the arrow.

That sounds reasonable, but it hides a real difficulty. Nobody knows the right velocity field in advance. There are infinitely many fields that would carry the fog onto the data, and even if you picked one, you could not write it down, because it depends on the whole data set at once. So how can you train a network to match a target you cannot compute?

# The trick that makes it trainable

This is where the paper earns its place. The authors notice that although the full field is out of reach, it is easy to write down a field for a single data point. Take one real example. Ask: what is a simple way to carry the fog onto just this one point? The answer can be almost embarrassingly simple. Draw a straight line from each noise sample to the example, and move along it at constant speed. The velocity along that line is just the difference between the end and the start. No integrals, no learned quantities, just a subtraction.

Now the key result. If you train the network to match these simple, single example velocities, averaged over all the examples and all the noise samples, the network ends up matching the full field you could never compute. The paper proves that the two training objectives have the same gradients, so minimising the easy one minimises the hard one. The authors call the easy version conditional flow matching, conditional because each target is conditioned on one data point.

It is worth pausing on why this works, because it is the heart of the paper. At any point in the fog, many different straight lines pass through, each heading to a different example. The network cannot know which line a particular particle is on. So the best it can do is predict the average of all the velocities of all the lines through that point. And that average, it turns out, is exactly the velocity of the full field. The network is forced to learn the average, and the average is the answer.

# Why not just use diffusion

You might ask why any of this is needed, since diffusion models already work. The honest answer is that flow matching is diffusion with the scaffolding removed. A diffusion model also moves noise onto data, but it describes the path through a noising process that runs forward and a learned reversal that runs back. That description brings along a lot of choices: a noise schedule, a variance for every step, a weighting of the loss at each noise level. Each of those choices matters, and each has been tuned by hand over years of work.

Flow matching asks you to choose only one thing: the path from noise to data for a single example. Choose the diffusion path, and you recover a diffusion model, with the same training signal written in a different form. Choose the straight line, and you get something new. The paper calls the straight line path optimal transport, borrowing a phrase from mathematics, because along a straight line each particle takes the most direct route and moves at constant speed.

The straight paths matter at sampling time. To generate a new example, you start from noise and follow the network's arrows, taking small steps. If the paths are curved, you need many small steps to follow them without flying off the curve. If they are nearly straight, you can take a few large steps and still land close to where you should. The paper shows that the optimal transport paths lead to fields that are much easier to follow, so the same quality comes out of far fewer network evaluations.

# What the training loop actually looks like

It helps to walk through one training step, because it is shorter than you might expect. First, pick a real example from the data. Second, draw a noise sample of the same shape. Third, pick a time between zero and one, uniformly at random. Fourth, find the point that lies that fraction of the way along the straight line from the noise to the example. Fifth, ask the network for the velocity at that point and that time. Sixth, compare its answer with the true velocity of the line, which is simply the example minus the noise, and nudge the network to make the difference smaller.

That is the whole algorithm. There is no simulation during training, no solving of equations, no sequence of steps to unroll. Each training example costs one forward pass and one backward pass of the network, just like ordinary supervised learning. This is a big part of why the method spread so quickly: it is easy to implement, easy to scale, and hard to get wrong.

There is one detail that the paper handles with care. The straight line from noise to data would end exactly on the data point, which makes the field singular at the very end. The authors add a tiny amount of spread, so that each path ends on a small blob around the example rather than on the point itself. In practice this width is very small, and it mostly exists to keep the mathematics well behaved.

# The results

The experiments are on image generation, the standard proving ground at the time. The authors train the same network architecture with three objectives: the usual diffusion objectives, flow matching with diffusion paths, and flow matching with optimal transport paths. Holding the network fixed is what makes the comparison fair. Any difference in the results comes from the objective and the path, not from a bigger model.

On the small image benchmarks, flow matching with optimal transport paths matches or beats the diffusion baselines on sample quality, measured by how closely the statistics of generated images match the real ones. The larger point is efficiency. To reach a given quality, the optimal transport model needs noticeably fewer function evaluations when generating, because its paths are straighter. On the larger benchmark, with images of the size people actually care about, the same pattern holds, and the training itself is more stable, with fewer of the loss spikes that diffusion training is known for.

The paper also reports likelihoods, the probability the model assigns to held out test images. Here too the flow matching models do well, which is a nice bonus: because the model is a continuous flow, you can compute exact likelihoods by integrating along the path, something that is awkward for many other generative models.

A figure in the paper makes the straight path argument visible. It shows sampling trajectories for a simple two dimensional example. The diffusion paths swing out in wide curves before settling on the data, while the optimal transport paths head almost directly to their destinations. You do not need the numbers once you have seen that picture.

# Where it falls short

The authors are fair about the limits. The straight line from noise to data is straight for each individual pair, but the overall field is still an average over many crossing lines, and that average can curve. So the promise of straight paths is only partly kept. Later work, which this paper inspired, tackles exactly this, by pairing noise samples with data points more cleverly so that the lines cross less, which makes the averaged field straighter still.

There is also the question of what the data looks like. Images live on a regular grid, and the straight line between two images is itself a sensible image, just blurry. For data with more structure, like molecules or crystals, a straight line between two configurations can pass through things that make no physical sense, such as atoms sitting on top of each other. Adapting the idea to those spaces, with paths that respect symmetry and geometry, is where a lot of the follow up work has gone, and it is why you now see flow matching in materials and chemistry papers as often as in image papers.

# Why it matters

Step back and the contribution is clear. The paper did not invent a new kind of network, and it did not find a new source of data. It found a cleaner way to state the training problem, one that turns an intractable target into an average of simple ones, and it showed that the cleaner statement lets you choose better paths. That combination, a simple loss and a free choice of path, is why so many recent generative models, for images, for video, for audio, and for molecules, are trained this way.

If you remember one sentence from this episode, make it this one. Flow matching trains a network to predict, at every point and every time, the average direction that the data is pulling, and it gets that average for free by training on one straight line at a time.

That is the paper. In the explainer page you will find the two dimensional trajectory figure and a side by side of the training loops for diffusion and flow matching, which makes the difference easy to see at a glance.

# A closer look at the paths

It is worth spending a little more time on the family of paths the paper allows, because the flexibility is easy to miss on a first reading. For each data point, the authors describe the path as a moving blob. At time zero the blob is the whole fog of noise. At time one it has shrunk down onto the data point. In between, two things change smoothly: where the centre of the blob sits, and how wide it is. Any choice of those two schedules gives a valid path, and each one comes with its own simple target velocity that you can write down directly.

The diffusion paths are one member of this family. In them the centre drifts toward the data slowly at first and then quickly, and the width shrinks in a particular curved way that comes from the noising process. The optimal transport path is another member, in which the centre moves along a straight line at constant speed and the width shrinks at a constant rate. Seen this way, the big debate about noise schedules in diffusion models becomes a question about which blob schedule to pick, and the paper makes the case that the simplest one is also one of the best.

This framing has a practical benefit that goes beyond images. When you move to a new kind of data, you do not have to reinvent the whole training recipe. You only have to decide what a sensible path from noise to a single example looks like in that space. For points on a sphere, you might move along great circles. For rotations, you might move along the shortest rotation that connects the two. For crystal lattices, you might move the atoms and the cell shape together in a way that respects the repeating structure. Each of these is a new path, but the training loop stays exactly the same.

# How sampling works in practice

Once the network is trained, generating a new example means solving a simple kind of equation: start at a noise sample and keep moving in the direction the network points, for a total time of one. In practice you do this with a numerical solver that takes a fixed number of steps, or one that adapts its step size to how sharply the path bends. The paper uses both, and reports how quality changes as the number of steps goes down.

This is where the straightness pays off most clearly. With a curved field, cutting the number of steps makes the solver cut corners, and the samples come out distorted. With a nearly straight field, even a handful of steps lands close to the right place. The paper's comparisons show that the optimal transport models degrade gracefully as you cut steps, while the diffusion models trained in the same setting fall off more steeply. For anyone who has to generate many samples, such as screening thousands of candidate materials, that difference translates directly into time and compute saved.

There is a subtle point here about what the network has learned. Because it predicts a velocity rather than a denoised image, the network is effectively learning a direction field over the whole space, not a sequence of separate cleanup steps. That makes it easy to reuse the same trained model with different solvers, different numbers of steps, or even to run it backwards, from data to noise, to measure how likely a given example is. All of these come from the same learned field.

# Connections worth knowing

The paper sits at a junction of several older ideas, and seeing them helps explain why it landed so well. The first is continuous normalising flows, an earlier class of models that also described generation as following a velocity field. Those models were elegant but painful to train, because every training step required simulating the flow from start to finish. Flow matching keeps the elegant description and removes the painful training, by never simulating the flow during training at all.

The second connection is to score matching, the idea behind diffusion models, in which a network learns the direction in which the data becomes more likely. The trick of training on simple conditional targets and recovering the full target in expectation is the same trick that makes denoising score matching work. The flow matching paper borrows that idea and applies it to velocities instead of scores, which is why the two families produce such similar results when you choose matching paths.

The third connection is optimal transport itself, a branch of mathematics about the cheapest way to move one distribution onto another. The paper's straight paths are optimal for each individual pair, but not for the distributions as a whole, which is exactly the gap that the later pairing methods try to close. Knowing this makes the follow up literature much easier to read: most of it is about getting closer to true optimal transport without paying its full cost.

# What to take away

To close, it helps to separate what is essential from what is incidental. The essential idea is that you can train a velocity field by regressing onto simple per example targets, and that the average of those targets is the field you want. The incidental choices are which paths you use, which network you train, and which solver you use to sample. The paper's main experimental message is that the straight, constant speed paths are a very good default, and that they buy you faster sampling without giving up quality.

For your own work, the relevant lesson is about where the difficulty goes. In flow matching, almost all of the design effort moves into choosing a path that makes sense for your data. For images, the straight line is fine. For structured scientific data, the path has to respect the rules of the space, and that is where most of the interesting modelling decisions now live.

That is the end of this episode. Next time, we will look at one of the papers that took these ideas into crystal structures, where the question of what a sensible path looks like becomes much more interesting.
