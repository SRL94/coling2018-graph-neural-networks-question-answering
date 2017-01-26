import logging
import itertools
import tqdm

from wikidata import entity_linking
from wikidata import wdaccess
from construction import stages, graph
from datasets import evaluation

generation_p = {
    'label.query.results': True,
    'logger': logging.getLogger(__name__),
}

logger = generation_p['logger']
logger.setLevel(logging.ERROR)


def generate_with_gold(ungrounded_graph, gold_answers):
    """
    Generate all possible groundings that produce positive f-score starting with the given ungrounded graph and
    using expand and restrict operations on its denotation.

    :param ungrounded_graph: the starting graph that should contain a list of tokens and a list of entities
    :param gold_answers: list of gold answers for the encoded question
    :return: a list of generated grounded graphs
    >>> max(g[1][2] if len(g) > 1 else 0.0 for g in generate_with_gold({'edgeSet': [], 'entities': [(['Nobel', 'Peace', 'Prize'], 'URL'), (['the', 'winner'], 'NN'), (['2009'], 'CD')]}, gold_answers=['barack obama']))
    1.0
    >>> max(g[1][2] if len(g) > 1 else 0.0 for g in generate_with_gold({'edgeSet': [], 'entities': [(['Texas', 'Rangers'], 'URL')], \
            'tokens': ['when', 'were', 'the', 'texas', 'rangers', 'started', '?']}, gold_answers=['1972']))
    1.0
    """
    ungrounded_graph = link_entities_in_graph(ungrounded_graph)
    pool = [(ungrounded_graph, (0.0, 0.0, 0.0), [])]  # pool of possible parses
    positive_graphs, negative_graphs = [], []
    iterations = 0
    while pool and (positive_graphs[-1][1][2] if len(positive_graphs) > 0 else 0.0) < 0.9:
        iterations += 1
        g = pool.pop(0)
        logger.debug("Pool length: {}, Graph: {}".format(len(pool), g))
        master_g_fscore = g[1][2]
        if master_g_fscore < 0.7:
            logger.debug("Restricting")
            restricted_graphs = stages.restrict(g[0])
            restricted_graphs = [add_canonical_labels_to_entities(r_g) for r_g in restricted_graphs]
            logger.debug("Suggested graphs: {}".format(restricted_graphs))
            chosen_graphs = []
            suggested_graphs = restricted_graphs[:]
            while not chosen_graphs and suggested_graphs:
                s_g = suggested_graphs.pop(0)
                chosen_graphs, not_chosen_graphs = ground_with_gold([s_g], gold_answers, min_fscore=master_g_fscore)
                negative_graphs += not_chosen_graphs
                logger.debug("Chosen graphs length: {}".format(len(chosen_graphs)))
                if not chosen_graphs:
                    logger.debug("Expanding")
                    expanded_graphs = stages.expand(s_g)
                    logger.debug("Expanded graphs (10): {}".format(expanded_graphs[:10]))
                    chosen_graphs, not_chosen_graphs = ground_with_gold(expanded_graphs, gold_answers, min_fscore=master_g_fscore)
                    negative_graphs += not_chosen_graphs
            if len(chosen_graphs) > 0:
                logger.debug("Extending the pool.")
                pool.extend(chosen_graphs)
            else:
                logger.debug("Extending the generated graph set: {}".format(g))
                positive_graphs.append(g)
        else:
            logger.debug("Extending the generated graph set: {}".format(g))
            positive_graphs.append(g)
    logger.debug("Iterations {}".format(iterations))
    logger.debug("Negative {}".format(len(negative_graphs)))
    return positive_graphs + negative_graphs


def link_entities_in_graph(ungrounded_graph):
    """
    Link all free entities in the graph.

    :param ungrounded_graph: graph as a dictionary with 'entities'
    :return: graph with entity linkings in the 'entities' array
    """
    entities = []
    for entity in ungrounded_graph.get('entities', []):
        if len(entity) == 2:
            linkings = entity_linking.link_entity(entity)
            entities.append(entity + (linkings,))
        else:
            entities.append(entity)
    ungrounded_graph['entities'] = entities
    return ungrounded_graph


def ground_with_gold(input_graphs, gold_answers, min_fscore=0.0):
    """
    For each graph among the suggested_graphs find its groundings in the WikiData, then evaluate each suggested graph
    with each of its possible groundings and compare the denotations with the answers embedded in the question_obj.
    Return all groundings that produce an f-score > 0.0

    :param input_graphs: a list of ungrounded graphs
    :param gold_answers: a set of gold answers
    :param min_fscore: lower bound on f-score for returned positive graphs
    :return: a list of graph groundings
    """
    logger.debug("Input graphs: {}".format(input_graphs))
    all_chosen_graphs, all_not_chosen_graphs = [], []
    input_graphs = input_graphs[:]
    while input_graphs and len(all_chosen_graphs) == 0:
        s_g = input_graphs.pop(0)
        chosen_graphs, not_chosen_graphs = ground_one_with_gold(s_g, gold_answers, min_fscore)
        all_chosen_graphs += chosen_graphs
        all_not_chosen_graphs += not_chosen_graphs
    all_chosen_graphs = sorted(all_chosen_graphs, key=lambda x: x[1][2], reverse=True)
    if len(all_chosen_graphs) > 3:
        all_chosen_graphs = all_chosen_graphs[:3]
    logger.debug("Number of chosen groundings: {}".format(len(all_chosen_graphs)))
    return all_chosen_graphs, all_not_chosen_graphs


def ground_one_with_gold(s_g, gold_answers, min_fscore):
    grounded_graphs = [apply_grounding(s_g, p) for p in find_groundings(s_g)]
    logger.debug("Number of possible groundings: {}".format(len(grounded_graphs)))
    logger.debug("First one: {}".format(grounded_graphs[:1]))
    retrieved_answers = [wdaccess.query_graph_denotations(s_g) for s_g in grounded_graphs]
    post_process_results = wdaccess.label_query_results if generation_p[
        'label.query.results'] else wdaccess.map_query_results
    retrieved_answers = [post_process_results(answer_set) for answer_set in retrieved_answers]
    logger.debug(
        "Number of retrieved answer sets: {}. Example: {}".format(len(retrieved_answers),
                                                                  retrieved_answers[0][:10] if len(
                                                                      retrieved_answers) > 0 else []))
    evaluation_results = [evaluation.retrieval_prec_rec_f1_with_altlabels(gold_answers, retrieved_answers[i]) for i in
                          range(len(grounded_graphs))]
    chosen_graphs = [(grounded_graphs[i], evaluation_results[i], retrieved_answers[i])
                     for i in range(len(grounded_graphs)) if evaluation_results[i][2] > min_fscore]
    not_chosen_graphs = [(grounded_graphs[i],) for i in range(len(grounded_graphs)) if evaluation_results[i][2] < 0.01]
    return chosen_graphs, not_chosen_graphs


def approximate_groundings(g):
    """
    Retrieve possible groundings for a given graph.
    The groundings are approximated by taking a product of groundings of the individual edges.

    :param g: the graph to ground
    :return: a list of graph groundings.
    >>> len(approximate_groundings({'edgeSet': [{'right': ['Percy', 'Jackson'], 'kbID': 'P179v', 'type': 'direct', 'hopUp': 'P674v', 'rightkbID': 'Q3899725'}, {'rightkbID': 'Q571', 'right': ['book']}], 'entities': []}))
    38
    """
    separate_groundings = []
    logger.debug("Approximating graph groundings: {}".format(g))
    for i, edge in enumerate(g.get('edgeSet', [])):
        if not('type' in edge and 'kbID' in edge):
            t = {'edgeSet': [edge]}
            edge_groundings = [apply_grounding(t, p) for p in wdaccess.query_graph_groundings(t, use_cache=True)]
            edge_groundings = [e for e in edge_groundings if "kbID" in e['edgeSet'][0] and e['edgeSet'][0]["kbID"][:-1] in wdaccess.property_whitelist]
            logger.debug("Edge groundings: {}".format(len(edge_groundings)))
            separate_groundings.append([p['edgeSet'][0] for p in edge_groundings])
        else:
            separate_groundings.append([edge])
    graph_groundings = []
    for edge_set in list(itertools.product(*separate_groundings)):
        new_g = graph.copy_graph(g)
        new_g['edgeSet'] = list(edge_set)
        graph_groundings.append(new_g)
    logger.debug("Graph groundings: {}".format(len(graph_groundings)))
    return graph_groundings


def find_groundings(g):
    """
    Retrieve possible groundings for a given graph.
    Doesn't work for complex graphs yet.

    :param g: the graph to ground
    :return: a list of graph groundings.
    """
    query_results = []
    num_edges_to_ground = sum(1 for e in g.get('edgeSet', []) if not('type' in e and 'kbID' in e))
    if not any('hopUp' in e or 'hopDown' in e for e in g.get('edgeSet', []) if not('type' in e and 'kbID' in e)):
        query_results += wdaccess.query_graph_groundings(g)
    else:
        edge_type_combinations = list(itertools.product(*[['direct', 'reverse']]*num_edges_to_ground))
        for type_combindation in edge_type_combinations:
            t = graph.copy_graph(g)
            for i, edge in enumerate([e for e in t.get('edgeSet', []) if not('type' in e and 'kbID' in e)]):
                edge['type'] = type_combindation[i]
            query_results += wdaccess.query_graph_groundings(t)
    if any(w in set(g.get('tokens', [])) for w in {'play', 'played', 'plays'}) and num_edges_to_ground == 1:
        t = graph.copy_graph(g)
        edge = [e for e in t.get('edgeSet', []) if not('type' in e and 'kbID' in e)][0]
        edge['type'] = 'v-structure'
        query_results += wdaccess.query_graph_groundings(t)
    return query_results


def find_groundings_with_gold(g):
    """
    Retrieve possible groundings for a given graph.
    Doesn't work for complex graphs yet.

    :param g: the graph to ground
    :return: a list of graph groundings.
    >>> len(find_groundings_with_gold({'edgeSet': [{'right': ['Percy', 'Jackson'], 'rightkbID': 'Q3899725'}, {'rightkbID': 'Q571', 'right': ['book']}]}))
    1
    """
    graph_groundings = []
    num_edges_to_ground = sum(1 for e in g.get('edgeSet', []) if not('type' in e and 'kbID' in e))
    edge_type_combinations = list(itertools.product(*[['direct', 'reverse']]*num_edges_to_ground))
    for type_combindation in edge_type_combinations:
        t = graph.copy_graph(g)
        for i, edge in enumerate([e for e in t.get('edgeSet', []) if not('type' in e and 'kbID' in e)]):
            edge['type'] = type_combindation[i]
        query_results = wdaccess.query_graph_groundings(t, use_cache=False, pass_exception=True)
        if query_results is None:
            appoximated_groundings = approximate_groundings(t)
            appoximated_groundings = [a for a in tqdm.tqdm(appoximated_groundings, ascii=True, disable=(logger.getEffectiveLevel() != logging.DEBUG)) if verify_grounding(a)]
            graph_groundings.extend(appoximated_groundings)
        else:
            graph_groundings.extend([apply_grounding(t, p) for p in query_results])
    return graph_groundings


def verify_grounding(g):
    """
    Verify the given graph with (partial) grounding exists in wikidata.

    :param g: graph as a dictionary
    :return: true if the graph exists, false otherwise
    """
    return wdaccess.query_wikidata(wdaccess.graph_to_ask(g))


def generate_without_gold(ungrounded_graph,
                          wikidata_actions=stages.WIKIDATA_ACTIONS, non_linking_actions=stages.NON_LINKING_ACTIONS):
    """
    Generate all possible groundings of the given ungrounded graph
    using expand and restrict operations on its denotation.

    :param ungrounded_graph: the starting graph that should contain a list of tokens and a list of entities
    :param wikidata_actions: optional, list of actions to apply with grounding in WikiData
    :param non_linking_actions: optional, list of actions to apply without checking in WikiData
    :return: a list of generated grounded graphs
    """
    pool = [ungrounded_graph]  # pool of possible parses
    wikidata_actions_restrict = wikidata_actions & set(stages.RESTRICT_ACTIONS)
    wikidata_actions_expand = wikidata_actions & set(stages.EXPAND_ACTIONS)
    generated_graphs = []
    iterations = 0
    while pool:
        if iterations % 10 == 0:
            logger.debug("Generated: {}".format(len(generated_graphs)))
            logger.debug("Pool: {}".format(len(pool)))
        g = pool.pop(0)
        # logger.debug("Pool length: {}, Graph: {}".format(len(pool), g))

        # logger.debug("Constructing with WikiData")
        suggested_graphs = [el for f in wikidata_actions_restrict for el in f(g)]
        suggested_graphs += [el for s_g in suggested_graphs for f in wikidata_actions_expand for el in f(s_g)]
        # pool.extend(suggested_graphs)
        # logger.debug("Suggested graphs: {}".format(suggested_graphs))
        # chosen_graphs = ground_without_gold(suggested_graphs)
        chosen_graphs = suggested_graphs
        # logger.debug("Extending the pool with {} graphs.".format(len(chosen_graphs)))
        pool.extend(chosen_graphs)
        # logger.debug("Label entities")
        chosen_graphs = [add_canonical_labels_to_entities(g) for g in chosen_graphs]

        # logger.debug("Constructing without WikiData")
        extended_graphs = [el for s_g in chosen_graphs for f in non_linking_actions for el in f(s_g)]
        chosen_graphs.extend(extended_graphs)

        # logger.debug("Extending the generated with {} graphs.".format(len(chosen_graphs)))
        generated_graphs.extend(chosen_graphs)
        iterations += 1
    logger.debug("Iterations {}".format(iterations))
    logger.debug("Generated: {}".format(len(generated_graphs)))
    generated_graphs = [g for g in tqdm.tqdm(generated_graphs, ascii=True, disable=(logger.getEffectiveLevel() != logging.DEBUG)) if verify_grounding(g)]
    logger.debug("Generated checked: {}".format(len(generated_graphs)))
    logger.debug("Clean up graphs.")
    for g in generated_graphs:
        if 'entities' in g:
            del g['entities']
    logger.debug("Grounding the resulting graphs.")
    generated_graphs = ground_without_gold(generated_graphs)
    # logger.debug("Approximated grounded graphs: {}".format(len(generated_graphs)))
    # generated_graphs = [g for g in tqdm.tqdm(generated_graphs, ascii=True, disable=(logger.getEffectiveLevel() != logging.DEBUG)) if verify_grounding(g)]
    logger.debug("Saved grounded graphs: {}".format(len(generated_graphs)))
    return generated_graphs


def ground_without_gold(input_graphs):
    """
    Construct possible groundings of the given graphs subject to a white list.

    :param input_graphs: a list of ungrounded graphs
    :return: a list of graph groundings
    """
    grounded_graphs = [p for s_g in tqdm.tqdm(input_graphs, ascii=True, disable=(logger.getEffectiveLevel() != logging.DEBUG)) for p in find_groundings_with_gold(s_g)]
    logger.debug("Number of possible groundings: {}".format(len(grounded_graphs)))
    logger.debug("First one: {}".format(grounded_graphs[:1]))

    grounded_graphs = [g for g in grounded_graphs if all(e.get("kbID")[:-1] in wdaccess.property_whitelist for e in g.get('edgeSet', []))]
    # chosen_graphs = [grounded_graphs[i] for i in range(len(grounded_graphs))]
    logger.debug("Number of chosen groundings: {}".format(len(grounded_graphs)))
    wdaccess.clear_cache()
    return grounded_graphs


def generate_with_model(ungrounded_graph, qa_model):

    return None


def apply_grounding(g, grounding):
    """
    Given a grounding obtained from WikiData apply it to the graph.
    Note: that the variable names returned by WikiData are important as they encode some grounding features.

    :param g: a single ungrounded graph
    :param grounding: a dictionary representing the grounding of relations and variables
    :return: a grounded graph
    >>> apply_grounding({'edgeSet':[{}]}, {'r0d':'P31v'}) == {'edgeSet': [{'type': 'direct', 'kbID': 'P31v', }], 'entities': []}
    True
    >>> apply_grounding({'edgeSet':[{}]}, {'r0v':'P31v'}) == {'edgeSet': [{'type': 'v-structure', 'kbID': 'P31v'}], 'entities': []}
    True
    >>> apply_grounding({'edgeSet':[{}]}, {'r0v':'P31v', 'hopup0v':'P131v'}) == {'edgeSet': [{'type': 'v-structure', 'kbID': 'P31v', 'hopUp':'P131v'}], 'entities': []}
    True
    >>> apply_grounding({'edgeSet': [{'type': 'v-structure', 'kbID': 'P31v', 'hopUp':'P131v'}], 'tokens': []}, {}) == {'edgeSet': [{'type': 'v-structure', 'kbID': 'P31v', 'hopUp':'P131v'}], 'entities': [], 'tokens': []}
    True
    >>> apply_grounding({'edgeSet':[{}, {}]}, {'r1d':'P39v', 'r0v':'P31v', 'e20': 'Q18'}) == {'edgeSet': [{'type': 'v-structure', 'kbID': 'P31v', 'rightkbID': 'Q18'}, {'type': 'direct', 'kbID': 'P39v'}], 'entities': []}
    True
    >>> apply_grounding({'edgeSet':[]}, {}) == {'entities': [], 'edgeSet': []}
    True
    """
    grounded = graph.copy_graph(g)
    for i, edge in enumerate(grounded.get('edgeSet', [])):
        if "e2" + str(i) in grounding:
            edge['rightkbID'] = grounding["e2" + str(i)]
        if "hop{}v".format(i) in grounding:
            if 'hopUp' in edge:
                edge['hopUp'] = grounding["hop{}v".format(i)]
            else:
                edge['hopDown'] = grounding["hop{}v".format(i)]
        if "r{}d".format(i) in grounding:
            edge['kbID'] = grounding["r{}d".format(i)]
            edge['type'] = 'direct'
        elif "r{}r".format(i) in grounding:
            edge['kbID'] = grounding["r{}r".format(i)]
            edge['type'] = 'reverse'
        elif "r{}v".format(i) in grounding:
            edge['kbID'] = grounding["r{}v".format(i)]
            edge['type'] = 'v-structure'

    return grounded


def add_canonical_labels_to_entities(g):
    """
    Label all the entities in the given graph that participate in relations with their canonical names.

    :param g: a graph as a dictionary with an 'edgeSet'
    :return: the original graph with added labels.
    """
    for edge in g.get('edgeSet', []):
        entitykbID = edge.get('rightkbID')
        if entitykbID and 'canonical_right' not in edge:
            entity_label = wdaccess.label_entity(entitykbID)
            if entity_label:
                edge['canonical_right'] = entity_label
    return g


if __name__ == "__main__":
    import doctest

    print(doctest.testmod())
