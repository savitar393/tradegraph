package id.tradegraph;

import java.util.Collection;
import java.util.Collections;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLEntity;
import org.semanticweb.owlapi.model.SWRLArgument;
import org.semanticweb.owlapi.model.SWRLAtom;
import org.semanticweb.owlapi.model.SWRLBuiltInAtom;
import org.semanticweb.owlapi.model.SWRLClassAtom;
import org.semanticweb.owlapi.model.SWRLDataPropertyAtom;
import org.semanticweb.owlapi.model.SWRLIndividualArgument;
import org.semanticweb.owlapi.model.SWRLObjectPropertyAtom;
import org.semanticweb.owlapi.model.SWRLRule;
import org.semanticweb.owlapi.model.SWRLVariable;

/** Syntax preflight for the comparison-only rule profile. */
final class RulePreflight {
    static final String SWRLB = "http://www.w3.org/2003/11/swrlb#";
    private static final Set<IRI> COMPARISONS = Set.of(
            IRI.create(SWRLB + "lessThan"),
            IRI.create(SWRLB + "lessThanOrEqual"),
            IRI.create(SWRLB + "greaterThan"),
            IRI.create(SWRLB + "greaterThanOrEqual"));

    enum Status { ACCEPTED, UNSUPPORTED }

    static Status check(SWRLRule rule) {
        var body = rule.getBody();
        var head = rule.getHead();
        if (body.isEmpty() || head.isEmpty() || !head.stream().allMatch(RulePreflight::isFactAtom)) {
            return Status.UNSUPPORTED;
        }

        var factPremises = body.stream().filter(RulePreflight::isFactAtom).toList();
        var boundData = dataVariables(factPremises);
        var boundIndividuals = individualVariables(factPremises);
        boolean supportedBody = body.stream().allMatch(atom ->
                isFactAtom(atom) || isBoundComparison(atom, boundData));
        return supportedBody
                && Collections.disjoint(boundData, boundIndividuals)
                && boundData.containsAll(dataVariables(head))
                && boundIndividuals.containsAll(individualVariables(head))
                ? Status.ACCEPTED : Status.UNSUPPORTED;
    }

    private static boolean isFactAtom(SWRLAtom atom) {
        boolean namedPredicate =
                (atom instanceof SWRLClassAtom type && !type.getPredicate().isAnonymous())
                || (atom instanceof SWRLObjectPropertyAtom object && !object.getPredicate().isAnonymous())
                || atom instanceof SWRLDataPropertyAtom;
        return namedPredicate && atom.getPredicate() instanceof OWLEntity entity && !entity.isBuiltIn()
                && atom.getAllArguments().stream().noneMatch(argument ->
                argument instanceof SWRLIndividualArgument individual
                        && individual.getIndividual().isAnonymous());
    }

    private static boolean isBoundComparison(SWRLAtom atom, Set<SWRLVariable> boundData) {
        return atom instanceof SWRLBuiltInAtom comparison
                && COMPARISONS.contains(comparison.getPredicate())
                && comparison.getArguments().size() == 2
                && boundData.containsAll(variables(comparison.getArguments().stream()));
    }

    private static Set<SWRLVariable> dataVariables(Collection<SWRLAtom> atoms) {
        return variables(atoms.stream()
                .filter(SWRLDataPropertyAtom.class::isInstance)
                .map(SWRLDataPropertyAtom.class::cast)
                .map(SWRLDataPropertyAtom::getSecondArgument));
    }

    private static Set<SWRLVariable> individualVariables(Collection<SWRLAtom> atoms) {
        return variables(atoms.stream().flatMap(atom -> atom instanceof SWRLDataPropertyAtom data
                ? Stream.of(data.getFirstArgument()) : atom.getAllArguments().stream()));
    }

    private static Set<SWRLVariable> variables(Stream<? extends SWRLArgument> arguments) {
        return arguments
                .filter(SWRLVariable.class::isInstance)
                .map(SWRLVariable.class::cast)
                .collect(Collectors.toSet());
    }
}
